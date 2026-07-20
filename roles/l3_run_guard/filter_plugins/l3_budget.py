"""umbrella #202 WP3 — launcher-tier authoritative capacity/lock/snapshot/
rollback logic (WP3-A/WP3-B, revised by codex round-1 R2a).

Pure functions only (mirrors roles/netbox_catalog_common/filter_plugins/netbox_tags.py):
offline-unit-testable via tests/l3-budget-logic.yml, no live cluster required.
Ansible tasks (roles/l3_run_guard/tasks/*.yml) are thin IO wrappers that feed
live k8s/helm-template/NetBox data into these functions and act on the result.

Scheduler-accurate per-pod demand (§3.2, plan grammar; R2a-6 corrected to
Kubernetes' REAL cumulative-prefix algorithm — see l3_pod_demand's own
docstring): NOT a flat "init max vs app-sum" the console's KSM-derived view
uses. Restartable sidecars (``initContainers[].restartPolicy == "Always"``,
K8s 1.28+) run concurrently with app containers for the pod's whole
lifetime, so their requests are CUMULATIVE with the app sum, and also
CUMULATIVE with every LATER sequential init container's own peak (a
sequential init that starts after a sidecar is already running pays for
both). This is only detectable from a live pod spec (or a rendered chart
template) — the console's Prometheus/KSM-derived supply view (umbrella #202
WP1) has no visibility into restartPolicy at all, so it can only ever
approximate with app+init-max. The launcher tier reads live/rendered specs
directly, so it implements the real distinction.

R2a-6a (fail-closed budget): a rendered chart's demand only counts as
"has_requests" (== a real per-run budget to gate capacity against) if EVERY
app and init container across EVERY rendered pod template declares BOTH a
parseable cpu AND a parseable memory request — one bare container next to
nine fully-budgeted ones still refuses missing-budget. See
``_container_requests``' presence tracking and ``l3_render_demand``.

ee_reserve is ALWAYS 0 in this module (plan §3.2 table): on the AWX-driven
path, the executing EE's own pod is already counted as part of "existing
demand" (it's a live pod on the node, scanned in the pod list the same as
everything else); on a direct run-playbook.sh invocation (ADR-0010 escape
hatch) there is no separate EE pod to reserve for at all. A caller that
also subtracts a nonzero ee_reserve on top of existing_demand would
double-count the EE's own footprint — see the no-double-count offline gate
in tests/l3-budget-logic.yml, which fixtures a running EE pod INTO
existing_demand and asserts FIT with ee_reserve=0 for a demand that
exactly fills remaining headroom (a double-counting implementation would
wrongly refuse).
"""

from __future__ import annotations

import hashlib
import re

_MEM_SUFFIXES = {
    "Ei": 1024 ** 6, "Pi": 1024 ** 5, "Ti": 1024 ** 4,
    "Gi": 1024 ** 3, "Mi": 1024 ** 2, "Ki": 1024,
    "E": 1000 ** 6, "P": 1000 ** 5, "T": 1000 ** 4,
    "G": 1000 ** 3, "M": 1000 ** 2, "k": 1000,
}
# Longest suffix first, so "Mi" is tried before a hypothetical single-char
# collision — matters because dict iteration order alone isn't length-sorted.
_MEM_SUFFIXES_BY_LEN = sorted(_MEM_SUFFIXES.items(), key=lambda kv: -len(kv[0]))

# Pod-template-bearing kinds a rendered chart can contain, and how to reach
# each one's PodSpec + effective replica count.
_WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Pod"}

# R2a-1 (P1-1): K8s label VALUE syntax — up to 63 chars, must start/end
# alphanumeric, internal chars may additionally be [-_.] ; empty string IS
# a legal label value. This is the VALUE grammar only (a label KEY has its
# own, stricter [prefix/]name grammar this module doesn't need — every
# label this role writes uses a fixed, hardcoded key name, only the VALUE
# is ever caller/data-derived).
_K8S_LABEL_VALUE_RE = re.compile(r"^(?:[A-Za-z0-9]([A-Za-z0-9_.-]{0,61}[A-Za-z0-9])?)?$")


def l3_valid_label_value(value) -> bool:
    """True if ``value`` is a legal Kubernetes label VALUE (R2a-1).

    A slash ('/') is illegal in a label value (it's reserved for the
    KEY's optional prefix/name separator) — this is exactly the bug a
    combined "<catalog_key>/<release>" single-label value hit (codex
    round-1 P1-1): split into two separate labels instead, each of which
    must independently pass this check. Used both as an offline proof
    (tests/l3-budget-logic.yml gate (k) — every label value this role
    writes, fixtured against this validator) and as the shared source of
    truth for what "legal" means here.
    """
    return bool(_K8S_LABEL_VALUE_RE.fullmatch(str(value)))


def _parse_cpu_millicores(value) -> int:
    """Parse a K8s CPU quantity ("500m", "2", "0.5") into millicores."""
    if value is None:
        return 0
    s = str(value).strip()
    if not s:
        return 0
    if s.endswith("m"):
        return int(round(float(s[:-1])))
    return int(round(float(s) * 1000))


def _parse_mem_bytes(value) -> int:
    """Parse a K8s memory quantity ("512Mi", "1Gi", "1000000000") into bytes."""
    if value is None:
        return 0
    s = str(value).strip()
    if not s:
        return 0
    for suffix, mult in _MEM_SUFFIXES_BY_LEN:
        if s.endswith(suffix):
            return int(round(float(s[: -len(suffix)]) * mult))
    return int(round(float(s)))


def _container_requests(container: dict) -> tuple:
    """Parsed (cpu_m, mem_b, has_both) for one container's resource
    requests. ``has_both`` (R2a-6a) is True only when BOTH the cpu and
    memory request keys are present and non-empty in the raw spec — a
    present-but-zero request ("0", 0) still counts as present (fail-closed
    cares about PRESENCE, not magnitude); an absent or empty-string value
    does not.
    """
    requests = ((container or {}).get("resources") or {}).get("requests") or {}
    cpu_raw = requests.get("cpu")
    mem_raw = requests.get("memory")
    has_cpu = cpu_raw is not None and str(cpu_raw).strip() != ""
    has_mem = mem_raw is not None and str(mem_raw).strip() != ""
    return _parse_cpu_millicores(cpu_raw), _parse_mem_bytes(mem_raw), (has_cpu and has_mem)


def l3_pod_demand(pod: dict) -> dict:
    """Scheduler-accurate demand (cpu_m, mem_b) for a single pod spec, PLUS
    ``all_containers_budgeted`` (R2a-6a fail-closed presence check).

    ``pod`` is a dict carrying at least a ``spec`` key (a full pod object,
    OR ``{"spec": <PodSpec>}`` — either works, only ``spec`` is read).

    R2a-6b: implements Kubernetes' REAL cumulative-prefix algorithm
    (upstream ``k8s.io/kubernetes/pkg/api/v1/resource.PodRequests``), not
    the WP3-A draft's "sequential-max vs concurrent-sum" approximation
    (which silently UNDER-counted whenever a sequential init container
    followed a restartable sidecar — codex's exact repro: sidecar 200m
    THEN sequential 900m, app 100m must total 1100m; the WP3-A draft
    returned 900m, dropping the sidecar's concurrent cost entirely).

    For each init container ``i`` in declaration ORDER, define::

        InitContainerUse(i) = sum(restartable inits with index < i) + req(i)

    (this applies uniformly whether container ``i`` itself is restartable
    or sequential — a restartable container's own request is included via
    the "index < i" sum evaluated one step later, since it becomes part of
    every SUBSEQUENT container's running total). Then::

        pod_demand = max(app_sum + sum(ALL restartable init requests),
                          max_i InitContainerUse(i)) + overhead

    This is ORDER-SENSITIVE by construction — the exact discriminating
    property codex's fixture proves (sidecar-before-sequential yields a
    different total than sequential-before-sidecar for the identical
    request values, since only inits BEFORE a given index contribute to
    that index's running prefix).
    """
    spec = (pod or {}).get("spec") or {}
    containers = spec.get("containers") or []
    init_containers = spec.get("initContainers") or []

    all_budgeted = True

    app_cpu = app_mem = 0
    for c in containers:
        cpu, mem, has_both = _container_requests(c)
        app_cpu += cpu
        app_mem += mem
        all_budgeted = all_budgeted and has_both

    running_restartable_cpu = running_restartable_mem = 0
    total_restartable_cpu = total_restartable_mem = 0
    init_max_cpu = init_max_mem = 0
    for c in init_containers:
        cpu, mem, has_both = _container_requests(c)
        all_budgeted = all_budgeted and has_both

        use_cpu = running_restartable_cpu + cpu
        use_mem = running_restartable_mem + mem
        init_max_cpu = max(init_max_cpu, use_cpu)
        init_max_mem = max(init_max_mem, use_mem)

        if c.get("restartPolicy") == "Always":
            running_restartable_cpu += cpu
            running_restartable_mem += mem
            total_restartable_cpu += cpu
            total_restartable_mem += mem

    concurrent_cpu = app_cpu + total_restartable_cpu
    concurrent_mem = app_mem + total_restartable_mem

    cpu_m = max(init_max_cpu, concurrent_cpu)
    mem_b = max(init_max_mem, concurrent_mem)

    overhead = spec.get("overhead") or {}
    cpu_m += _parse_cpu_millicores(overhead.get("cpu"))
    mem_b += _parse_mem_bytes(overhead.get("memory"))

    return {"cpu_m": cpu_m, "mem_b": mem_b, "all_containers_budgeted": all_budgeted}


def l3_existing_demand(pods: list, node_name: str) -> dict:
    """Sum scheduler-accurate demand for every pod BOUND to ``node_name``.

    "Bound" = ``spec.nodeName`` set to this node, phase Running OR Pending
    (a Pending-but-bound pod — e.g. still pulling its image — still holds
    its scheduler reservation on the node; only phase matters, not
    readiness). Pods not yet bound to any node (unscheduled Pending) never
    count against this specific node's headroom.
    """
    total_cpu = total_mem = 0
    for pod in pods or []:
        spec = (pod or {}).get("spec") or {}
        status = (pod or {}).get("status") or {}
        if spec.get("nodeName") != node_name:
            continue
        if status.get("phase") not in ("Running", "Pending"):
            continue
        demand = l3_pod_demand(pod)
        total_cpu += demand["cpu_m"]
        total_mem += demand["mem_b"]
    return {"cpu_m": total_cpu, "mem_b": total_mem}


def l3_node_allocatable(nodes: list) -> dict:
    """The single node's allocatable capacity, or a multi-node refusal marker.

    Returns ``{"ok": True, "name", "cpu_m", "mem_b"}`` for exactly one node,
    else ``{"ok": False, "count": N}`` — the caller decides how a non-single
    node count is handled (this module never refuses on the caller's
    behalf, it just reports the fact).
    """
    nodes = nodes or []
    if len(nodes) != 1:
        return {"ok": False, "count": len(nodes)}
    node = nodes[0]
    alloc = ((node or {}).get("status") or {}).get("allocatable") or {}
    return {
        "ok": True,
        "name": ((node or {}).get("metadata") or {}).get("name"),
        "cpu_m": _parse_cpu_millicores(alloc.get("cpu")),
        "mem_b": _parse_mem_bytes(alloc.get("memory")),
    }


def _pod_template_spec(obj: dict, kind: str):
    if kind in ("Deployment", "StatefulSet", "DaemonSet", "Job"):
        return ((obj.get("spec") or {}).get("template") or {}).get("spec")
    if kind == "CronJob":
        job_tmpl = ((obj.get("spec") or {}).get("jobTemplate") or {})
        return (((job_tmpl.get("spec") or {}).get("template") or {}).get("spec"))
    if kind == "Pod":
        return obj.get("spec")
    return None


def _replica_count(obj: dict, kind: str):
    """How many pod-instances' worth of demand this workload object
    contributes. Returns a non-negative int, or ``None`` to signal an
    unsupported/unparseable shape — the caller (``l3_render_demand``)
    treats ``None`` the SAME as a missing per-container request: fail
    closed (``all_budgeted=False``), never silently defaulting to 1.

    R4a (codex round-3 P2-1): the R2a/R3a draft's ``max(1, replicas)``
    (PLUS an ``or 1`` that ALSO turned a genuine ``replicas: 0`` into 1)
    floored every Deployment/StatefulSet demand at "at least one pod's
    worth", even for a deliberately zero-scaled workload — codex's exact
    repro: a direct probe charged 100m/64Mi to a Deployment with
    ``replicas: 0``. Job/CronJob were hardcoded to 1 pod regardless of
    ``spec.parallelism`` (Job) / ``spec.jobTemplate.spec.parallelism``
    (CronJob), under-counting any parallel Job.
    """
    if kind in ("Deployment", "StatefulSet"):
        raw = (obj.get("spec") or {}).get("replicas", 1)
        raw = 1 if raw is None else raw
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return None
        return n if n >= 0 else None
    if kind == "Job":
        raw = (obj.get("spec") or {}).get("parallelism", 1)
        raw = 1 if raw is None else raw
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return None
        return n if n >= 0 else None
    if kind == "CronJob":
        job_spec = ((obj.get("spec") or {}).get("jobTemplate") or {}).get("spec") or {}
        raw = job_spec.get("parallelism", 1)
        raw = 1 if raw is None else raw
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return None
        return n if n >= 0 else None
    if kind in ("DaemonSet", "Pod"):
        # DaemonSet/Pod: single-node assumption means one pod instance's
        # worth of demand is what matters here — neither kind has a
        # replicas/parallelism concept to under/over-count.
        return 1
    return None


def l3_render_demand(manifests: list) -> dict:
    """Sum scheduler-accurate demand across every workload object a
    rendered chart (``helm template`` output, parsed via the ansible
    ``from_yaml_all`` filter) declares.

    Returns ``{"cpu_m", "mem_b", "has_requests"}``. R2a-6a (fail-closed):
    ``has_requests`` is True only when the chart renders AT LEAST ONE
    pod-template-bearing object AND EVERY container (app + init) in EVERY
    rendered pod template has BOTH cpu and memory requests present — one
    fully-budgeted container next to one bare container still refuses
    missing-budget (codex's exact repro fixture: two containers, one
    budgeted, one bare — the WP3-A draft's "any nonzero total" rule missed
    this entirely since the budgeted container's nonzero sum masked the
    bare one).

    R4a (codex round-3 P2-1): the SAME fail-closed posture now also
    covers replica/parallelism accounting — a workload whose
    ``_replica_count`` returns ``None`` (an unparseable or negative
    replicas/parallelism value; see that function's own docstring) marks
    ``all_budgeted`` False for the WHOLE render, the identical mechanism
    an under-specified container already triggers. A genuine
    ``replicas: 0`` / ``parallelism: 0`` is NOT this case — it's a valid,
    honestly-zero demand and contributes 0 to the total, never floored to
    "at least one pod's worth."
    """
    total_cpu = total_mem = 0
    any_workload = False
    all_budgeted = True
    for obj in manifests or []:
        if not isinstance(obj, dict):
            continue
        kind = obj.get("kind")
        if kind not in _WORKLOAD_KINDS:
            continue
        pod_spec = _pod_template_spec(obj, kind)
        if not pod_spec:
            continue
        any_workload = True
        demand = l3_pod_demand({"spec": pod_spec})
        replicas = _replica_count(obj, kind)
        if replicas is None:
            all_budgeted = False
            continue
        all_budgeted = all_budgeted and demand["all_containers_budgeted"]
        total_cpu += demand["cpu_m"] * replicas
        total_mem += demand["mem_b"] * replicas
    return {"cpu_m": total_cpu, "mem_b": total_mem, "has_requests": any_workload and all_budgeted}


def l3_evaluate_fit(allocatable: dict, existing_demand: dict, ee_reserve: dict, run_demand: dict) -> dict:
    """FIT/NO-FIT verdict + a §3.4-style human-legible report (node name
    only — no IPs, per the plan's public-safety posture).

    ``ee_reserve`` is expected to be ``{"cpu_m": 0, "mem_b": 0}`` on every
    real caller — see the module docstring's no-double-count note; kept as
    an explicit parameter (not hardcoded) so the offline gate can prove
    the zero-value flows through untouched rather than assuming it.
    """
    ee_cpu = (ee_reserve or {}).get("cpu_m", 0)
    ee_mem = (ee_reserve or {}).get("mem_b", 0)
    headroom_cpu = allocatable["cpu_m"] - existing_demand["cpu_m"] - ee_cpu
    headroom_mem = allocatable["mem_b"] - existing_demand["mem_b"] - ee_mem
    fit = headroom_cpu >= run_demand["cpu_m"] and headroom_mem >= run_demand["mem_b"]
    verdict = "fit" if fit else "no-fit"

    lines = [
        "L3 launcher capacity preflight (§3.2, scheduler-accurate, single node)",
        f"node={allocatable.get('name', '?')} verdict={verdict}",
        f"cpu(m): demand={run_demand['cpu_m']} headroom={headroom_cpu} "
        f"(allocatable={allocatable['cpu_m']} existing={existing_demand['cpu_m']} ee_reserve={ee_cpu})",
        f"mem(B): demand={run_demand['mem_b']} headroom={headroom_mem} "
        f"(allocatable={allocatable['mem_b']} existing={existing_demand['mem_b']} ee_reserve={ee_mem})",
    ]
    if verdict == "no-fit":
        lines.append(
            "shortfall: cpu={0}m mem={1}B".format(
                max(0, run_demand["cpu_m"] - headroom_cpu),
                max(0, run_demand["mem_b"] - headroom_mem),
            )
        )

    return {
        "verdict": verdict,
        "headroom_cpu_m": headroom_cpu,
        "headroom_mem_b": headroom_mem,
        "report": "\n".join(lines),
    }


def l3_lock_decision(existing_lock, now_epoch, ttl_seconds) -> dict:
    """Facility-lock decision given an existing lock's ConfigMap ``data``
    (or ``None``/``{}`` if absent — those are equivalent to "no lock").

    R2a-3b (codex round-1 P1-3, final disposition): NO within-TTL reclaim
    EVER, not even by the SAME run_id. This DROPS the WP3-A/A2
    same-run-residue reclaim carve-out entirely — two attempts of the same
    run are still two DIFFERENT attempt_ids (R2a-3a: attempt_id is now a
    separate, freshly-minted identity per play invocation, distinct from
    run_id), and the §4.5 retry promise is defined as "after the prior
    attempt's own terminal release or the lock's TTL expiry" — never as
    "silently steal the lock because the run_id matches," which could race
    a genuinely still-in-flight prior attempt of the same run. Every
    within-TTL hold, regardless of who holds it, is now a plain refusal.

    Returns one of:
      * ``{"action": "create"}`` — no lock exists, safe to create.
      * ``{"action": "reclaim", "stale_holder", "stale_attempt_id",
        "age_seconds", "reason": "ttl-expired"}`` — the stale lock should
        be deleted (via a PRECONDITIONED delete — R2a-3c, see lock.yml) and
        a fresh one create-only'd in its place.
      * ``{"action": "refuse", "holder", "run_id", "age_seconds"}`` — held
        by ANYONE, within TTL — genuine facility-busy refusal (R2a-3d: NOT
        override-able).
    """
    if not existing_lock:
        return {"action": "create"}
    try:
        created_at = int(existing_lock.get("created_at", 0))
    except (TypeError, ValueError):
        created_at = 0
    age = int(now_epoch) - created_at

    if age > int(ttl_seconds):
        return {
            "action": "reclaim",
            "stale_holder": existing_lock.get("holder"),
            "stale_attempt_id": existing_lock.get("holder_attempt_id"),
            "age_seconds": age,
            "reason": "ttl-expired",
        }
    return {
        "action": "refuse",
        "holder": existing_lock.get("holder"),
        "run_id": existing_lock.get("run_id"),
        "age_seconds": age,
    }


def l3_lock_release_decision(current_lock_data, my_attempt_id) -> dict:
    """R2a-3c/R2a-3d decision table: may THIS attempt delete the currently
    -held lock? The single source of truth for the "who may release"
    rule lock_release.yml consults (never a blind delete — see its own
    header comment for the full "never delete a lock you don't own"
    rationale).

    ``current_lock_data`` is ``None``/falsy when no lock object was found
    on the fresh pre-delete read at all (nothing to release — already
    gone). Returns ``{"permitted": bool, "reason": str}``; ``permitted``
    is True ONLY when the lock's own recorded ``holder_attempt_id``
    exactly matches ``my_attempt_id`` — a mismatch means this attempt's
    own hold already expired (TTL) and someone else reclaimed it, so
    deleting it now would destroy a DIFFERENT attempt's legitimate lock.
    """
    if not current_lock_data:
        return {"permitted": False, "reason": "no-lock-present"}
    if current_lock_data.get("holder_attempt_id") == my_attempt_id:
        return {"permitted": True, "reason": "owned-by-this-attempt"}
    return {"permitted": False, "reason": "owned-by-a-different-attempt"}


# R3a-5 (codex round-2 P1-5): the FULL rollback-eligible status set — a
# widened supersede rule. The WP3-A/R2a draft only superseded records
# still ``in-progress``, leaving ``failed_rollback_required`` and
# ``rollback_incomplete`` residue eligible for rollback FOREVER even after
# a later launch/teardown already invalidated their baseline — this
# contradicted the load-bearing "a later mutating run invalidates earlier
# snapshots" invariant l3_snapshot_refusal's own docstring describes.
_L3_ROLLBACK_ELIGIBLE_STATUSES = frozenset({"in-progress", "failed_rollback_required", "rollback_incomplete"})


def l3_classify_run_records(all_run_cms: list, current_run_id: str) -> dict:
    """Classify ``dmf-run-*`` ConfigMaps: the CURRENT run's own record (if
    already present — a re-read case) vs every OTHER record still in a
    ROLLBACK-ELIGIBLE status (R3a-5: widened from ``in-progress``-only to
    the full ``_L3_ROLLBACK_ELIGIBLE_STATUSES`` set — the lock guarantees
    no genuinely concurrent run holds any of these statuses at the moment
    a NEW mutating run acquires it, so ALL of them are dead-run residue
    from this new run's perspective, safe to mark ``superseded``).

    Used by ``_supersede_sweep.yml``, shared between the launch flow
    (called right after snapshot.yml's own create) and lock_only.yml's
    teardown guard (called right after lock acquire — a teardown has no
    snapshot stage of its own, but invalidates other runs' baselines the
    same way a launch's own mutation does).
    """
    current = None
    supersede_candidates = []
    for cm in all_run_cms or []:
        labels = ((cm or {}).get("metadata") or {}).get("labels") or {}
        data = (cm or {}).get("data") or {}
        run_id = labels.get("dmf.io/run-id")
        if run_id == current_run_id:
            current = cm
        elif data.get("status") in _L3_ROLLBACK_ELIGIBLE_STATUSES:
            supersede_candidates.append(cm)
    return {"current": current, "supersede_candidates": supersede_candidates}


def l3_netbox_surface(results: list) -> list:
    """Extract the snapshot's NetBox attribution surface (name, tag names,
    custom fields) from a raw NetBox services API response's ``results``
    list — keeps jmespath/json_query out of the task files, consistent
    with "decision logic lives in filter plugins". Superseded by
    ``l3_netbox_surface_with_id`` for the snapshot/rollback flow (R2a-5
    needs the record ``id`` for by-id classification) — kept for API
    stability, no longer called by any task file in this role.
    """
    out = []
    for svc in results or []:
        tags = [t.get("name") for t in (svc.get("tags") or []) if isinstance(t, dict)]
        out.append({
            "name": svc.get("name"),
            "tags": tags,
            "custom_fields": svc.get("custom_fields") or {},
        })
    return out


def l3_monitoring_surface(netbox_surface: list) -> list:
    """Derive the monitoring attribution surface from the NetBox surface
    (``l3_netbox_surface_with_id``'s output shape works too — only ``name``/
    ``tags``/``custom_fields`` are read, any extra ``id`` key is ignored)
    — probe-tag presence + the monitoring custom fields, NOT a live PromSD
    read (see snapshot.yml's header comment for why).
    """
    out = []
    for entry in netbox_surface or []:
        out.append({
            "name": entry.get("name"),
            "has_probe_tag": "monitoring:probe" in (entry.get("tags") or []),
            "custom_fields": entry.get("custom_fields") or {},
        })
    return out


def l3_set_flag_argv(set_values: list) -> list:
    """Flatten ``["k1=v1", "k2=v2"]`` into repeated ``["--set", "k1=v1", ...]``
    argv pairs for a ``helm template``/``helm upgrade`` command-module argv
    list — keeps the awkward interleaving out of Jinja task files.
    """
    argv = []
    for v in set_values or []:
        argv.extend(["--set", v])
    return argv


def l3_set_values_dicts(set_values: list) -> list:
    """Turn ``["k1=v1", "k2=v2"]`` into ``kubernetes.core.helm``'s own
    ``set_values`` module-parameter shape (``[{"value": "k1=v1",
    "value_type": "raw"}, ...]``) — ``value_type: raw`` matches plain
    ``helm --set`` semantics exactly, the same interpretation
    ``l3_set_flag_argv``'s ``--set`` argv gets from the helm CLI.

    umbrella #202 WP3 R6a (codex round-5, true single-source): this is the
    INSTALL-side twin of ``l3_set_flag_argv`` — both are one-line, direct
    transformations of the SAME flat ``l3_chart_set_values`` list, so a
    launch playbook's ``kubernetes.core.helm`` install task and
    ``capacity.yml``'s ``helm template`` render consume the identical
    source values in each one's own required shape, with no second,
    independently-authored representation (e.g. a hand-built nested
    ``release_values`` dict) that could silently diverge from it.
    """
    return [{"value": v, "value_type": "raw"} for v in set_values or []]


def l3_flatten_set_values(values: dict, _prefix: str = "") -> list:
    """Flatten a nested ``release_values``-shaped dict (as used by
    ``kubernetes.core.helm``'s ``release_values``) into dotted-path
    ``"key.sub=value"`` strings matching plain ``helm --set`` syntax — so a
    playbook using the nested-dict form (nmos-cpp, nmos-crosspoint) can
    still produce the SAME ``l3_chart_set_values`` list shape that
    ``helm template`` (and the ``l3_set_flag_argv`` above) expects, without
    hand-duplicating the values in two different shapes.
    """
    out = []
    for key, value in (values or {}).items():
        path = f"{_prefix}{key}"
        if isinstance(value, dict):
            out.extend(l3_flatten_set_values(value, f"{path}."))
        elif isinstance(value, bool):
            out.append(f"{path}={str(value).lower()}")
        else:
            out.append(f"{path}={value}")
    return out


# ═══════════════════════════════════════════════════════════════════════
# WP3-B — rollback-run pure logic (plan §4.1/§4.2/§4.4/§4.5/§4.6), revised
# by codex round-1 (R2a-5, R2a-7)
# ═══════════════════════════════════════════════════════════════════════

# §4.1: a run's snapshot ConfigMap status that means the snapshot is STALE
# for rollback purposes — the run already reached a terminal outcome that
# makes a subtractive diff against this snapshot unsafe (a superseded
# snapshot's "pre-existing" baseline may no longer reflect reality; a
# run_complete run has nothing left to roll back — NOTE rollback_complete
# is no longer a member: R2a-7 makes it structurally unreachable in WP3,
# so there is nothing to remove it FROM here; it never gets written).
_STALE_SNAPSHOT_STATUSES = frozenset({"run_complete", "superseded"})
# Statuses a rollback may legitimately proceed against — including its OWN
# prior state (failed_rollback_required — the normal trigger) and
# rollback_incomplete (the §4.5 idempotent retry case — the ONLY terminal
# rollback status in WP3, per R2a-7).
_PROCEED_SNAPSHOT_STATUSES = frozenset({"in-progress", "failed_rollback_required", "rollback_incomplete"})


def l3_snapshot_refusal(snapshot_status) -> str:
    """Classify a run's snapshot status for rollback eligibility (§4.1).

    Returns a refusal token (``"no-snapshot"``/``"stale-snapshot"``) or
    ``None`` if the rollback should proceed. ``snapshot_status`` is
    ``None`` when no snapshot ConfigMap was found at all (the caller reads
    a k8s_info result and passes ``None`` for zero resources found).

    An UNRECOGNIZED status (anything outside both known sets — a status
    string introduced by a future WP this function doesn't know about
    yet) fails closed to ``stale-snapshot`` rather than silently
    proceeding against a status it doesn't understand — never assume
    "unknown" means "safe".
    """
    if snapshot_status is None:
        return "no-snapshot"
    if snapshot_status in _STALE_SNAPSHOT_STATUSES:
        return "stale-snapshot"
    if snapshot_status in _PROCEED_SNAPSHOT_STATUSES:
        return None
    return "stale-snapshot"


def l3_helm_release_diff(pre_run_releases: list, current_releases: list) -> list:
    """Releases attributable to a run (§4.4): present NOW but NOT in the
    pre-run snapshot — these were created BY the run's own provision stage
    and should be removed on rollback. Compares by release ``name`` (each
    item a ``helm list -o json`` entry dict, or the snapshot's own stored
    copy of one).
    """
    pre_names = {r.get("name") for r in (pre_run_releases or [])}
    return [r for r in (current_releases or []) if r.get("name") not in pre_names]


def l3_releases_to_remove(pre_run_helm_surface: dict, current_helm_surface: dict) -> dict:
    """Per-namespace wrapper around ``l3_helm_release_diff`` — both
    surfaces are shaped ``{"mxl": [...], "nmos": [...]}`` (WP3-A
    snapshot.yml's own ``_l3_helm_surface`` shape).
    """
    namespaces = set(pre_run_helm_surface or {}) | set(current_helm_surface or {})
    return {
        ns: l3_helm_release_diff(
            (pre_run_helm_surface or {}).get(ns, []),
            (current_helm_surface or {}).get(ns, []),
        )
        for ns in namespaces
    }


def l3_flatten_releases_to_remove(releases_to_remove: dict) -> list:
    """Flatten ``{"mxl": [{"name": "a"}], "nmos": [{"name": "b"}]}`` into
    ``[{"namespace": "mxl", "release": "a"}, {"namespace": "nmos",
    "release": "b"}]`` — a single flat loop target for the uninstall task.
    """
    out = []
    for namespace, releases in (releases_to_remove or {}).items():
        for r in releases or []:
            name = r.get("name") if isinstance(r, dict) else r
            if name:
                out.append({"namespace": namespace, "release": name})
    return out


def l3_helm_content_identity(helm_surface: dict, values_results: list) -> dict:
    """R3a-8 (codex round-2 P1-8): attach a CONTENT-IDENTITY fingerprint —
    ``values_hash`` — to each release in a helm surface (``{"mxl":[...],
    "nmos":[...]}``, the shape both snapshot.yml and
    rollback_helm_surface.yml build). Combined with the ``chart`` (already
    ``<name>-<version>`` in every raw ``helm list -o json`` entry) and
    ``app_version`` fields (also already present), this gives a full
    content-identity triple used INSTEAD of the release's own ``revision``
    number for restore verification — see ``l3_helm_content_restores``'s
    docstring for why revision-number comparison is a structural
    false-positive machine (``helm rollback`` always MINTS a new revision
    number, it never reverts the counter).

    ``values_results`` is a list of Ansible per-release loop-result dicts
    (each carrying ``item`` — the original ``{"name","namespace",...}``
    entry this iteration was for — and ``stdout``, the raw
    ``helm get values -o json`` text, or a failed/empty result); matched
    back onto the surface by ``(namespace, name)``. A missing/failed
    result yields ``values_hash: None`` for that release.

    R4a (codex round-3 P1-8): also attaches ``values_fetch_failed``
    (``True``/``False``) per release, DISTINCT from ``values_hash`` being
    ``None``. The R3a-8 draft only ever set ``values_hash: None`` on a
    failed fetch and relied on every DOWNSTREAM comparison to treat
    ``None`` as "doesn't match" — but ``None == None`` is still equal in
    Python, so TWO INDEPENDENTLY-failed values fetches (e.g. the snapshot
    capture's own fetch failed for a release, AND the later readback's
    fetch also failed for the same release) compared equal and reported
    clean, a false-negative codex reproduced directly. Every caller must
    now check ``values_fetch_failed`` explicitly wherever it treats
    ``values_hash`` as authoritative — a fetch failure is never "no
    change", it's "unknown", and unknown must never read as clean.
    """
    by_key = {}
    for r in values_results or []:
        item = (r or {}).get("item") or {}
        key = (item.get("namespace"), item.get("name"))
        stdout = (r or {}).get("stdout") or ""
        failed = bool((r or {}).get("failed", False)) or not stdout
        by_key[key] = (None if failed else hashlib.sha256(stdout.encode()).hexdigest(), failed)

    out = {}
    for ns, releases in (helm_surface or {}).items():
        enriched = []
        for rel in releases or []:
            rel = dict(rel or {})
            value_hash, fetch_failed = by_key.get((ns, rel.get("name")), (None, True))
            rel["values_hash"] = value_hash
            rel["values_fetch_failed"] = fetch_failed
            enriched.append(rel)
        out[ns] = enriched
    return out


def l3_helm_content_restores(pre_run_helm_surface: dict, current_helm_surface: dict) -> list:
    """Releases attributable to R3a-8 (replaces the R2a-10i draft's
    revision-number comparison with CONTENT identity): present in BOTH the
    pre-run snapshot AND current state (pre-existing — NOT this run's own
    creation, see ``l3_helm_release_diff`` for the create side) whose
    chart name-version, app_version, or values hash CHANGED between
    snapshot-time and now — this run's own provision stage re-deployed
    (helm-upgraded) an ALREADY-existing release, and a failed run must
    roll that upgrade back.

    Revision-number comparison (the R2a-10i draft) is a STRUCTURAL
    false-positive machine here: ``helm rollback <rel> N`` always MINTS a
    NEW, higher revision number recording the rollback ITSELF as a fresh
    history entry — Helm's own documented behavior (a rollback-to-2 shows
    up as current revision 3, not "back to 2") — so comparing the
    POST-ROLLBACK current revision against the PRE-RUN snapshot's revision
    would NEVER match, permanently flagging every successful restore as
    dirty. Content identity doesn't have this problem: a successful
    rollback's chart/app_version/values converge to the snapshot's
    recorded triple regardless of what the revision COUNTER reads.

    Still returns ``snapshot_revision`` (the target for the actual
    ``helm rollback <rel> <snapshot_revision>`` call — a revision NUMBER
    is operationally required to know which history entry to roll back
    TO, even though it's never used for verification) alongside the
    content-identity fields the READBACK step (``l3_helm_readback_dirty``)
    verifies against.

    R4a (codex round-3 P1-8): a ``values_fetch_failed`` flag on EITHER
    side unconditionally forces this release into the restore-target
    list — a failed fetch means content identity is UNKNOWN, not
    "unchanged", so it must never be silently skipped. ``pre`` carrying
    ``values_fetch_failed`` is a defensive/legacy-snapshot case in
    practice (snapshot.yml now refuses fail-closed at capture time rather
    than ever persisting one — see snapshot.yml's own header), kept here
    so this function stays correct even against an older snapshot.
    """
    out = []
    namespaces = set(pre_run_helm_surface or {}) | set(current_helm_surface or {})
    for ns in namespaces:
        pre_by_name = {r.get("name"): r for r in (pre_run_helm_surface or {}).get(ns, [])}
        for cur in (current_helm_surface or {}).get(ns, []):
            name = cur.get("name")
            pre = pre_by_name.get(name)
            if pre is None:
                continue  # not pre-existing — l3_helm_release_diff handles create-side removal instead
            if (
                cur.get("values_fetch_failed")
                or pre.get("values_fetch_failed")
                or cur.get("chart") != pre.get("chart")
                or cur.get("app_version") != pre.get("app_version")
                or cur.get("values_hash") != pre.get("values_hash")
            ):
                out.append({
                    "namespace": ns,
                    "release": name,
                    "snapshot_revision": pre.get("revision"),
                    "snapshot_chart": pre.get("chart"),
                    "snapshot_app_version": pre.get("app_version"),
                    "snapshot_values_hash": pre.get("values_hash"),
                })
    return out


def l3_helm_readback_dirty(
    pre_run_helm_surface: dict,
    targeted_removals: list,
    readback_surface: dict,
    listing_failed: bool,
) -> bool:
    """R2a-7 §4.6-honest readback verification: call-success never equals
    clean. Only a FRESH ``helm list`` (+ per-release values fetch, R3a-8)
    taken AFTER the uninstall/rollback loops proves the surface actually
    converged — neither call's own success/failure is authoritative (an
    "already gone" idempotent success, a slow API, a partial apply can all
    diverge from live state). A listing failure itself counts as dirty
    (never assumed-clean on a failed read).

    Checked, all against the FULL pre-run snapshot:

      * every ``targeted_removals`` entry (``l3_flatten_releases_to_remove``'s
        output shape) is ACTUALLY GONE from the readback.
      * EVERY OTHER ``pre_run_helm_surface`` release (everything NOT in
        ``targeted_removals``) is STILL PRESENT in the readback, AND its
        chart/app_version/values_hash still matches what the SNAPSHOT
        recorded for it — compared DIRECTLY against the snapshot's own
        fields, not scoped to a separately-computed restore-target list.

    R4a (codex round-3 P1-8, second finding): the R3a-8 draft only
    content-compared releases already in a caller-supplied
    ``content_restores`` list (``l3_helm_content_restores``'s output —
    releases whose content had ALREADY diverged at CLASSIFICATION time,
    before this rollback's own mutations ran). A release that was
    IDENTICAL to its snapshot at classification time but drifted DURING
    this rollback (e.g. a concurrent actor, or a partial/racing helm
    operation) was invisible to that scoped check and reported clean.
    This function no longer takes a ``content_restores`` argument at all
    — it compares every non-removed pre-existing release's content
    directly against the snapshot's own recorded fields, unconditionally.
    A ``values_fetch_failed`` flag on EITHER side (the original snapshot
    capture, or this readback) is treated as dirty outright — unknown
    content identity is never "clean" (see l3_helm_content_identity's own
    docstring for why a bare ``values_hash: None`` alone isn't enough).
    """
    if listing_failed:
        return True

    removal_keys = {(t.get("namespace"), t.get("release")) for t in targeted_removals or []}
    for target in targeted_removals or []:
        ns, name = target.get("namespace"), target.get("release")
        present_names = {r.get("name") for r in (readback_surface or {}).get(ns, [])}
        if name in present_names:
            return True

    for ns, releases in (pre_run_helm_surface or {}).items():
        readback_by_name = {r.get("name"): r for r in (readback_surface or {}).get(ns, [])}
        for rel in releases or []:
            name = rel.get("name")
            key = (ns, name)
            if key in removal_keys:
                continue  # supposed to be gone — already checked above
            if rel.get("values_fetch_failed"):
                return True  # the SNAPSHOT baseline itself is untrustworthy for this release
            current = readback_by_name.get(name)
            if current is None:
                return True  # a pre-existing release silently vanished
            if current.get("values_fetch_failed"):
                return True  # the readback couldn't confirm content — never assume clean
            if (
                current.get("chart") != rel.get("chart")
                or current.get("app_version") != rel.get("app_version")
                or current.get("values_hash") != rel.get("values_hash")
            ):
                return True
    return False


def l3_netbox_records_to_restore_or_delete(pre_run_netbox_surface: list, current_netbox_records: list) -> dict:
    """Classify NOW-current NetBox records against the pre-run snapshot
    (§4.2), by RECORD ID (R2a-5 — codex round-1 P1-5), never by name alone.

    A current record whose ``id`` was ALREADY in the pre-run snapshot
    pre-existed this run — restore it to exactly what the snapshot
    recorded (tags + custom_fields), never re-derive a fresh
    "bootstrapped" state that might not match what was actually there
    before. A record whose ``id`` is NOT in the snapshot was created BY
    this run's own provision stage — finalise it (same PATCH shape as a
    normal teardown) THEN delete it entirely, so a rolled-back run leaves
    no orphan catalog entry behind.

    R2a-5: comparing by NAME alone is unsafe — if the pre-existing record
    was deleted and a NEW record created (by this run's own provision)
    reusing the SAME name, a name-only comparison would misclassify the
    new (run-created) record as "restore" (pre-existing) purely because
    the name matches, silently preserving a record that should have been
    torn down. Comparing by ``id`` catches this structurally: a delete-then
    -recreate always produces a NEW id, so the classification is correct
    regardless of name reuse.

    R2a-5's OTHER fix (upstream of this function, in snapshot.yml/
    rollback_netbox_surface.yml): the snapshot itself must be captured by
    EXACT NAME (the same identity the provision stage's own lookup uses),
    not by tag — a pre-existing record that hadn't yet been tagged
    ``app:<key>`` at snapshot time would be INVISIBLE to a tag-based
    snapshot query, so it would never even reach this function's
    ``pre_run_netbox_surface`` input at all, and would be wrongly
    classified as run-created (delete) purely because the snapshot never
    saw it. This function only fixes the classification given a CORRECT
    snapshot input — see tests/l3-budget-logic.yml gate (f) for the
    upstream regression fixture proving name-based capture matters too.

    ``current_netbox_records`` and ``pre_run_netbox_surface`` items are
    both ``l3_netbox_surface_with_id``'s output shape (id + name + tags +
    custom_fields) — the snapshot flow now captures WITH id (R2a-5), so
    both sides of this comparison carry it.

    R3a-7 (codex round-2 P1-7): iterates the SNAPSHOT's OWN ids, not just
    CURRENT records — the R2a-5 draft only ever walked
    ``current_netbox_records``, so a snapshotted id that's ABSENT from
    current entirely (deleted by something else during the run, with
    nothing reusing its name) was invisible to the classifier: an empty
    current list produced ``restore=[], delete=[]`` and the rollback
    declared clean despite an unrestorable record. Returns a THIRD list,
    ``missing`` — snapshot records whose id no longer exists at all,
    unrestorable by definition (there's no PATCH target), which the
    caller must treat as dirty and name in the outcome surfaces. Also
    naturally covers the "same-name-different-id" case: the OLD id being
    gone shows up in ``missing`` (dirty) even as the NEW id (never in the
    snapshot) shows up in ``delete`` (finalised+removed) — both fire
    independently, from the same by-id comparison.
    """
    pre_ids = {r.get("id"): r for r in (pre_run_netbox_surface or [])}
    current_by_id = {r.get("id"): r for r in (current_netbox_records or [])}

    restore = []
    missing = []
    for pre_id, pre_rec in pre_ids.items():
        cur = current_by_id.get(pre_id)
        if cur is None:
            missing.append(pre_rec)
        else:
            restore.append({"current": cur, "snapshot": pre_rec})

    delete = [rec for rec in (current_netbox_records or []) if rec.get("id") not in pre_ids]

    return {"restore": restore, "delete": delete, "missing": missing}


def l3_netbox_surface_with_id(results: list) -> list:
    """Same extraction as ``l3_netbox_surface`` but WITH the NetBox
    record's own ``id`` carried through — needed by R2a-5's by-id
    classification (both the pre-run snapshot capture AND the rollback
    flow's current-state read now use this, not the id-less variant).
    """
    out = []
    for svc in results or []:
        tags = [t.get("name") for t in (svc.get("tags") or []) if isinstance(t, dict)]
        out.append({
            "id": svc.get("id"),
            "name": svc.get("name"),
            "tags": tags,
            "custom_fields": svc.get("custom_fields") or {},
        })
    return out


def l3_filter_owned_tags(tag_names: list, owned_namespace: list) -> list:
    """Filter a flat tag-NAME list down to just the OWNED-namespace subset
    (ADR-0046's owned/non-owned split — mirrors
    ``netbox_catalog_common/filter_plugins/netbox_tags.py``'s private
    ``_is_owned`` helper; duplicated rather than cross-imported since
    Ansible filter plugins are role-scoped, not shared modules).

    Returns ``[{"name": "..."}]`` dicts — the shape
    ``merge_owned_tags``'s ``desired_tags`` parameter expects. Used to
    turn a SNAPSHOTTED record's full tag list (owned + non-owned, e.g.
    ``workload:*``) into just the owned subset before handing it to
    ``merge_owned_tags`` as what THIS restore wants to own — passing the
    unfiltered snapshot tags through directly would re-introduce a STALE
    non-owned tag value (e.g. an old ``workload:*``) alongside whatever
    the record's CURRENT non-owned tags already are, duplicating instead
    of correctly deferring to current state for anything not owned here.
    """
    prefixes = [p for p in (owned_namespace or []) if p.endswith(":")]
    exacts = {p for p in (owned_namespace or []) if not p.endswith(":")}

    def _is_owned(name: str) -> bool:
        if name in exacts:
            return True
        return any(name.startswith(p) for p in prefixes)

    return [{"name": n} for n in (tag_names or []) if _is_owned(n)]


def l3_filter_owned_custom_fields(custom_fields: dict, owned_field_names: list) -> dict:
    """R4a (codex round-3 P1-10): filter a custom_fields dict down to just
    the L3-OWNED monitoring key subset (the same key set
    ``l3_netbox_monitoring_custom_fields_clear`` names — probe_module,
    probe_path, cluster_service, cluster_namespace, cluster_port). Used
    by ``_rollback_netbox_restore_one.yml`` to turn a SNAPSHOTTED
    record's full custom_fields map into just the owned subset before
    PATCHing it back: NetBox's custom_fields PATCH is a server-side
    shallow merge (confirmed by ``_rollback_netbox_delete_one.yml``,
    which already sends only the clear-fields subset with no prior read),
    so sending the unfiltered snapshot dict would blow away any NON-owned
    custom field (e.g. an operator/other-controller-owned field) a human
    or another controller changed on the record after the snapshot was
    taken — reverting it to stale data. Sending just this filtered subset
    lets NetBox's own merge behavior leave every non-owned field
    untouched (current-preserved), the same anti-stale-ownership posture
    already applied to tags via ``l3_filter_owned_tags``/
    ``merge_owned_tags``.

    Also doubles as the readback comparison's own ``desired_custom_fields``
    input (``l3_netbox_restore_dirty``) — since that function only checks
    the keys present in whatever dict it's given, passing this SAME
    filtered subset there for free makes the readback compare only the
    L3-owned keys too (never a non-owned field this restore never wrote).
    """
    owned = set(owned_field_names or [])
    return {k: v for k, v in (custom_fields or {}).items() if k in owned}


def l3_finalise_tags_for_delete(current_tags: list, owned_namespace: list) -> list:
    """Compute the owned-desired tag set for a run-created NetBox record
    about to be deleted (§4.2) — the same SHAPE a normal teardown finalise
    stage sends (lifecycle flipped to bootstrapped, monitoring stamps
    dropped), derived from what the record ALREADY carries (this
    launcher-agnostic role has no per-catalog-entry tag defaults of its
    own to reconstruct fresh values from) rather than guessing at
    catalog-specific tag values it was never given.
    """
    owned = l3_filter_owned_tags(current_tags, owned_namespace)
    kept = [t for t in owned if not t["name"].startswith("monitoring:") and not t["name"].startswith("lifecycle:")]
    kept.append({"name": "lifecycle:bootstrapped"})
    return kept


def l3_netbox_restore_dirty(
    desired_owned_tags: list,
    desired_custom_fields: dict,
    readback_record,
    read_failed: bool,
    owned_namespace: list,
) -> bool:
    """R2a-7 §4.6-honest readback verification for a RESTORED (pre-existing)
    NetBox record: call-success never equals clean. A PATCH 200 only
    proves NetBox ACCEPTED the request, not that a subsequent read
    reflects it — this confirms the OWNED tag subset (ADR-0046) and the
    snapshot's custom_fields actually landed, via a fresh GET taken after
    the restore PATCH.

    R3a-7 (codex round-2 P1-7): EXACT owned-tag-set equality, not
    subset-only. The R2a-7 draft only checked that every DESIRED owned tag
    was PRESENT in the readback — it never checked for a STALE owned tag
    that shouldn't be there anymore (codex's exact repro: desired=
    {lifecycle:bootstrapped}, readback carries
    {lifecycle:bootstrapped, monitoring:probe} — the old subset check said
    clean despite the stale monitoring:probe tag still lingering). This
    filters the readback's OWN full tag list down to just its
    owned-namespace subset (via ``l3_filter_owned_tags``, using the SAME
    ``owned_namespace`` the desired set was itself filtered with) and
    compares the two sets for EXACT equality — non-owned tags are never
    touched by this comparison at all (current-preserved, by design;
    that's the whole point of ``merge_owned_tags``).

    ``desired_owned_tags`` is ``l3_filter_owned_tags``'s own output shape
    (``[{"name": "..."}]``); ``readback_record`` is the fresh GET's raw
    NetBox JSON body (``{"tags": [{"name": ...}, ...], "custom_fields": {...}}``)
    or ``None``/falsy if the read itself failed.
    """
    if read_failed or not isinstance(readback_record, dict):
        return True
    readback_all_tag_names = [
        t.get("name") for t in (readback_record.get("tags") or []) if isinstance(t, dict)
    ]
    readback_owned_names = {t["name"] for t in l3_filter_owned_tags(readback_all_tag_names, owned_namespace)}
    desired_names = {
        (t.get("name") if isinstance(t, dict) else t) for t in (desired_owned_tags or [])
    }
    if readback_owned_names != desired_names:
        return True
    readback_cf = readback_record.get("custom_fields") or {}
    for key, value in (desired_custom_fields or {}).items():
        if readback_cf.get(key) != value:
            return True
    return False


def l3_netbox_delete_dirty(readback_status_code, read_failed: bool) -> bool:
    """R2a-7 §4.6-honest readback verification for a run-created NetBox
    record's DELETE: the record must actually 404 on a fresh GET taken
    after the DELETE call — a 200 (still there) or any other outcome means
    the delete didn't really take, regardless of what the DELETE call
    itself reported as its own status.
    """
    if read_failed:
        return True
    return readback_status_code != 404


def l3_rollback_outcome(helm_dirty: bool, netbox_dirty: bool) -> dict:
    """Aggregate per-surface rollback outcomes into the terminal verdict.

    R2a-7 (§4.6-honest amendment, codex round-1 P1-7 final disposition):
    monitoring drain is STRUCTURALLY UNVERIFIABLE from the launcher EE — no
    live PromSD read exists anywhere in this tier (see snapshot.yml's own
    monitoring-surface header comment; this was already true in WP3-A/B,
    R2a-7 just stops pretending otherwise at the terminal-status layer).
    Because of that, ``monitoring`` is now ALWAYS a member of the dirty-
    surfaces list, on EVERY rollback, regardless of how clean the helm/
    netbox readbacks came back — which makes ``verdict="rollback_complete"``
    STRUCTURALLY UNREACHABLE in WP3 (grep-provable: this function never
    returns that string, on any input). WP4's console-side verification is
    the only tier that can ever upgrade a run to fully clean — the umbrella
    #202 acceptance gate closes only with WP4, not this WP.

    ``helm_dirty``/``netbox_dirty`` now come from READBACK verification
    (``l3_helm_readback_dirty``/``l3_netbox_restore_dirty``/
    ``l3_netbox_delete_dirty``), never from a mutation call's own bare
    success/failure — see each's own docstring.
    """
    dirty_surfaces = []
    if helm_dirty:
        dirty_surfaces.append("helm")
    if netbox_dirty:
        dirty_surfaces.append("netbox")
    dirty_surfaces.append("monitoring")
    return {"verdict": "rollback_incomplete", "surfaces": dirty_surfaces}


def l3_any_failed(results: list) -> bool:
    """True if any item in an Ansible ``loop``+``register`` result list
    (each with a ``failed`` key from ``ignore_errors: true`` tolerance)
    failed — a shared helper for call-level (not readback-level) failure
    detection, still used where a call's own failure is informative even
    though it's no longer authoritative for dirty-state determination
    (R2a-7 — readback is authoritative; a call failure is logged, not
    load-bearing).
    """
    for item in results or []:
        if isinstance(item, dict) and item.get("failed"):
            return True
    return False


class FilterModule:
    """Ansible filter plugin entry point."""

    def filters(self):
        return {
            "l3_valid_label_value": l3_valid_label_value,
            "l3_pod_demand": l3_pod_demand,
            "l3_existing_demand": l3_existing_demand,
            "l3_node_allocatable": l3_node_allocatable,
            "l3_render_demand": l3_render_demand,
            "l3_evaluate_fit": l3_evaluate_fit,
            "l3_lock_decision": l3_lock_decision,
            "l3_lock_release_decision": l3_lock_release_decision,
            "l3_classify_run_records": l3_classify_run_records,
            "l3_netbox_surface": l3_netbox_surface,
            "l3_monitoring_surface": l3_monitoring_surface,
            "l3_set_flag_argv": l3_set_flag_argv,
            "l3_set_values_dicts": l3_set_values_dicts,
            "l3_flatten_set_values": l3_flatten_set_values,
            "l3_snapshot_refusal": l3_snapshot_refusal,
            "l3_helm_release_diff": l3_helm_release_diff,
            "l3_releases_to_remove": l3_releases_to_remove,
            "l3_flatten_releases_to_remove": l3_flatten_releases_to_remove,
            "l3_helm_content_identity": l3_helm_content_identity,
            "l3_helm_content_restores": l3_helm_content_restores,
            "l3_helm_readback_dirty": l3_helm_readback_dirty,
            "l3_netbox_records_to_restore_or_delete": l3_netbox_records_to_restore_or_delete,
            "l3_netbox_surface_with_id": l3_netbox_surface_with_id,
            "l3_filter_owned_tags": l3_filter_owned_tags,
            "l3_filter_owned_custom_fields": l3_filter_owned_custom_fields,
            "l3_finalise_tags_for_delete": l3_finalise_tags_for_delete,
            "l3_netbox_restore_dirty": l3_netbox_restore_dirty,
            "l3_netbox_delete_dirty": l3_netbox_delete_dirty,
            "l3_rollback_outcome": l3_rollback_outcome,
            "l3_any_failed": l3_any_failed,
        }
