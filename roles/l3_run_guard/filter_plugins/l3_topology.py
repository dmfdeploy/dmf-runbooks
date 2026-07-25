"""umbrella #201 WP3 — topology_params projection (dmf-media
catalog/topology-params.schema.yaml, WP1 frozen contract).

Pure functions only (same posture as l3_budget.py): offline-unit-testable,
no live cluster required. Ansible tasks (roles/l3_run_guard/tasks/
topology_validate.yml, capacity.yml, snapshot.yml, rollback_netbox_surface.yml)
are thin IO wrappers that feed the topology_params object (carried as AWX
launch extra_vars, per the umbrella spec §3.2 seam) into these functions.

Validation mirrors dmf-cms's own ``catalog.load_topology_instance``
(cms-wp3a-201 branch, src/dmf_cms/catalog.py) field-for-field — the launcher
is the SECOND fail-closed gate on this object (the console's WP3a seam is
the first), so both consumers judge the object by the same rules. Kept as
an independent Python re-implementation, not a cross-repo import (Ansible
filter plugins are role-scoped; dmf-cms is a different repo/runtime
entirely).
"""

from __future__ import annotations

import uuid


def _err(msg: str) -> dict:
    return {"ok": False, "error": msg}


def l3_validate_topology_params(topology_params) -> dict:
    """Fail-closed structural validation of a ``topology_params`` object
    (WP1 schema_version 1). Returns ``{"ok": True}`` or
    ``{"ok": False, "error": "<reason>"}`` — never raises, so a task file
    can gate a refusal on ``.ok`` without wrapping this in its own
    block/rescue.

    Enforces the full WP1 frozen contract: schema_version == 1; sources is
    a non-empty list with unique ids, each entry has id/flow_id/pattern,
    flow_id is a well-formed UUID and unique across sources[] (compared by
    PARSED uuid.UUID value, not the raw string — two spellings of the same
    UUID in different case are the SAME flow identity, mirroring
    dmf-cms's own P1 re-gate finding), pattern is distinct across
    sources[]; viewer.source_selection references a real sources[].id;
    target_facility is present. This is a STRUCTURAL check only — it does
    NOT resolve/validate target_facility against a live NetBox site (that
    is topology_validate.yml's own live agreement check, a separate
    concern this pure function has no access to).
    """
    if not isinstance(topology_params, dict):
        return _err("topology_params must be a mapping")

    if topology_params.get("schema_version") != 1:
        return _err(
            f"schema_version must equal 1, got {topology_params.get('schema_version')!r}"
        )

    sources = topology_params.get("sources")
    if not isinstance(sources, list) or not sources:
        return _err("sources must be a non-empty list")

    ids: list[str] = []
    parsed_flow_ids: list[uuid.UUID] = []
    patterns: list[str] = []
    for i, s in enumerate(sources):
        if not isinstance(s, dict):
            return _err(f"sources[{i}] is not a mapping")
        sid = s.get("id")
        if not sid or not isinstance(sid, str):
            return _err(f"sources[{i}].id missing/invalid")
        flow_id = s.get("flow_id")
        if not flow_id or not isinstance(flow_id, str):
            return _err(f"sources[{i}].flow_id missing/invalid")
        try:
            parsed_flow_id = uuid.UUID(flow_id)
        except ValueError:
            return _err(f"sources[{i}].flow_id {flow_id!r} is not a well-formed UUID")
        pattern = s.get("pattern")
        if not pattern or not isinstance(pattern, str):
            return _err(f"sources[{i}].pattern missing/invalid")
        ids.append(sid)
        parsed_flow_ids.append(parsed_flow_id)
        patterns.append(pattern)

    if len(ids) != len(set(ids)):
        return _err("sources[].id values are not unique")
    if len(parsed_flow_ids) != len(set(parsed_flow_ids)):
        return _err("sources[].flow_id values are not unique")
    if len(patterns) != len(set(patterns)):
        return _err(
            "sources[].pattern values are not distinct — the switch must be "
            "visually unambiguous (spec §4)"
        )

    viewer = topology_params.get("viewer")
    if not isinstance(viewer, dict):
        return _err("viewer must be a mapping")
    if not viewer.get("id") or not isinstance(viewer.get("id"), str):
        return _err("viewer.id missing/invalid")
    source_selection = viewer.get("source_selection")
    if not source_selection or not isinstance(source_selection, str):
        return _err("viewer.source_selection missing/invalid")
    if source_selection not in ids:
        return _err(
            f"viewer.source_selection {source_selection!r} does not reference "
            f"a known sources[].id {ids}"
        )

    if not topology_params.get("target_facility") or not isinstance(
        topology_params.get("target_facility"), str
    ):
        return _err("target_facility missing/invalid")

    return {"ok": True}


def l3_topology_release_group(topology_params: dict, l3_release: str, base_set_values: list) -> list:
    """Derive the per-source release group (umbrella #201 WP3, spec §3.2/
    §5) from a VALIDATED ``topology_params`` — caller must have already
    gated on ``l3_validate_topology_params`` returning ``ok: True``, this
    function does not re-validate.

    NAMING RULE (umbrella #201 WP3 fix-round D5 — state this explicitly, the
    tests must prove THIS shipping contract, not an alternate one): one entry
    per ``sources[]`` item, N-shaped (never a 2-source special case).
    ``release``/``netbox_service_name`` are both
    ``"{{ l3_release }}-{{ source.id }}"`` — and on the RATIFIED viewer JT
    (launch-mxl-fabrics-demo.yml's view-role invocation, l3_release=
    "mxl-videotest-view", the only launch that ever calls this function
    live — see topology_validate.yml's own header) this concretely means
    ``mxl-videotest-view-source-a`` / ``mxl-videotest-view-source-b`` for
    J1's two sources. dmf-media's viewer catalog entry must author
    ``provision.netbox_service`` as the matching N-element list of exactly
    these names (umbrella #201 ledger, WP1 follow-up) — drain.py already
    resolves an N-element list natively (condition-3 finding, WP3 report).
    ``chart_set_values`` is
    ``base_set_values`` (the caller's own common values — interface, image
    tag, placement mode, etc., IDENTICAL to what the viewer's own scalar
    install already threads) PLUS the three topology-contract projections
    named by the WP1 seam doc (docs/topology-params-seam.md): ``role=source``
    (a source release is always role=source, regardless of what role this
    PLAYBOOK RUN itself is — the group is threaded from the co-running
    viewer launch, see topology_validate.yml's own header), ``flow.id``,
    ``pattern``, ``sourceId`` — the chart's own value names (WP2
    charts/mxl-fabrics-demo/values.yaml), which happen to match the
    topology schema's field names 1:1 except for the id->sourceId rename
    (the chart's ``sourceId`` compares against the coordinator's
    ``active-source``, so it cannot literally be named ``id`` without
    colliding with Helm's own reserved template namespace).
    """
    group = []
    for source in topology_params["sources"]:
        release = f"{l3_release}-{source['id']}"
        chart_set_values = list(base_set_values or []) + [
            "role=source",
            f"flow.id={source['flow_id']}",
            f"pattern={source['pattern']}",
            f"sourceId={source['id']}",
        ]
        group.append({
            "release": release,
            "netbox_service_name": release,
            "source_id": source["id"],
            "chart_set_values": chart_set_values,
        })
    return group


def l3_topology_viewer_projection(topology_params: dict) -> dict:
    """The viewer-side projection of a VALIDATED ``topology_params``
    (spec §5 step 3): the coordinator's ``active-source`` value (verbatim
    ``viewer.source_selection``) and the viewer's OWN ``flow.id`` — looked
    up as ``sources[<source_selection>].flow_id``, NOT left on whichever
    source's flow the viewer happened to start with (the WP1 schema's own
    ``viewer.source_selection.projection`` note: an implementation that
    sets active-source alone, without also re-deriving the viewer's
    flow.id from this lookup, leaves the viewer receiving the wrong
    baseline flow after a switch).

    Returns ``{"active_source": <source id>, "flow_id": <that source's
    flow_id>}``. Caller must have already validated topology_params —
    the referential integrity of source_selection (it names a real
    sources[].id) is exactly what l3_validate_topology_params already
    proved, so the lookup below cannot miss.
    """
    selection = topology_params["viewer"]["source_selection"]
    flow_id = next(s["flow_id"] for s in topology_params["sources"] if s["id"] == selection)
    return {"active_source": selection, "flow_id": flow_id}


def l3_topology_service_names(l3_catalog_key: str, release_group) -> list:
    """The full NetBox service-name set a topology-shaped run's snapshot/
    rollback surfaces must query — this run's OWN scalar identity
    (``l3_catalog_key``, unchanged: the viewer, spec settled decision "+1
    viewer release unchanged") PLUS every group item's
    ``netbox_service_name`` (the N sources), when a group is defined.

    ``release_group`` is ``None``/undefined/empty for every non-topology
    launch (every other catalog entry, and a topology run's OWN source-role
    invocation, which never derives a group — see topology_validate.yml's
    header) — the returned list then degrades to ``[l3_catalog_key]``,
    the SAME single-name list snapshot.yml/rollback_netbox_surface.yml
    already queried before this WP, byte-identical.
    """
    names = [l3_catalog_key]
    for item in release_group or []:
        names.append(item["netbox_service_name"])
    return names


def l3_sum_demand(demands: list) -> dict:
    """Combine N per-release ``l3_render_demand``-shaped results
    (``{"cpu_m", "mem_b", "has_requests"}``) into one budget total for
    ``l3_evaluate_fit`` — sums cpu_m/mem_b across every release; a
    topology-shaped run's demand is genuinely the SUM of the viewer's own
    render plus every group source's render, since Kubernetes schedules
    them as N+1 independent workloads on the same shared node budget.

    ``has_requests`` is the AND across every entry, not the OR — the
    SAME fail-closed posture ``l3_render_demand`` already applies within
    ONE chart render (R2a-6a: one under-budgeted container anywhere fails
    the whole render); here it is the identical rule one level up, across
    releases: one under-budgeted RELEASE anywhere fails the whole run's
    budget, never silently averaged away by N-1 fully-budgeted releases.

    A single-element list (the byte-identical non-group case — see
    ``l3_topology_service_names``'s own docstring for the same "degrades to
    today" pattern) returns that element's own cpu_m/mem_b/has_requests
    completely unchanged, so this is a safe drop-in even when nothing
    upstream of it derived a group at all.
    """
    total_cpu = total_mem = 0
    all_budgeted = True
    for d in demands or []:
        total_cpu += d["cpu_m"]
        total_mem += d["mem_b"]
        all_budgeted = all_budgeted and bool(d["has_requests"])
    return {"cpu_m": total_cpu, "mem_b": total_mem, "has_requests": all_budgeted}


class FilterModule:
    """Ansible filter plugin entry point."""

    def filters(self):
        return {
            "l3_validate_topology_params": l3_validate_topology_params,
            "l3_topology_release_group": l3_topology_release_group,
            "l3_topology_viewer_projection": l3_topology_viewer_projection,
            "l3_topology_service_names": l3_topology_service_names,
            "l3_sum_demand": l3_sum_demand,
        }
