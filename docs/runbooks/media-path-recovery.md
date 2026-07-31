# Media-Path Recovery via Cluster Truth

**Scope:** recovering a stuck or frozen MXL media path (viewer preview dead,
source supervisors parked) by reading and writing the coordinator
ConfigMap and the facility lock directly with `kubectl`.

**Orchestration-backend-agnostic by design.** Every command in this runbook
operates on cluster truth — the `mxl-coordinator` ConfigMap in the `mxl`
namespace and the `dmf-facility-lock` ConfigMap in the `nmos` namespace — not
on any AWX job, dmf-cms operation record, or job template. It works whether
the stuck workload was deployed by an AWX-driven catalog launch, a direct
`run-playbook.sh` invocation (ADR-0010), or (for diagnosis only) neither.
Nothing here assumes AWX is the current orchestration backend, and nothing
here should ever grow an AWX-specific dependency — AWX is one exchangeable
backend, not a premise of this ladder.

**Prerequisite:** a working `kubectl` context pointed at the cluster that
hosts the stuck workload. Verify with `kubectl config current-context`
before running anything below — this runbook never names a concrete
context, cluster, or env id (those live in operator-local state, not this
public repo).

## Vocabulary this runbook assumes

From `playbooks/switch-mxl-fabrics-demo.yml` and
`roles/l3_run_guard/tasks/switch_*.yml` (read these for the authoritative
sequencing — this section only orients):

- **`mxl-coordinator`** — a ConfigMap in the `mxl` namespace with three keys:
  `active-source`, `target-info`, `epoch`. Source supervisors gate
  transmission on the **full conjunction of all three being present**
  (fail-closed) — any one missing and the matching supervisor stays parked.
- **`active-source`** is the only one of the three an operator can
  meaningfully hand-restore. It just selects which already-running,
  already-advertised source a parked supervisor should un-park for.
- **`target-info`** and **`epoch`** are **instance-minted** — republished by
  the viewer/target pod's own publisher **only at container startup**. There
  is no operator write that fabricates a valid value for either; the only
  way to get fresh ones is to make the target pod start.
- A switch's own quiesce (PHASE 1) clears all three keys; re-point (PHASE 2)
  restarts the target Deployment (named `<receiver_instance>-target`, e.g.
  `mxl-videotest-view-target` for `receiver_instance: mxl-videotest-view`),
  whose fresh pod republishes `target-info`/`epoch`; select (PHASE 3) sets
  `active-source`. This runbook exists for when that sequence is interrupted
  partway and the automatic path (rollback-and-verify, or the console) did
  not complete it.

## The decision ladder

Cheapest rung first. **Always start at Rung 0** — the coordinator's own
state tells you which rung applies; do not guess.

### Rung 0 — verify cluster truth

```bash
kubectl -n mxl get cm mxl-coordinator -o jsonpath='{.data}'
```

A healthy coordinator returns all three keys with non-empty values, e.g.
`{"active-source":"...","epoch":"...","target-info":"..."}`. An unhealthy
one is missing one or more. Read the *combination* off this one call before
doing anything else:

| `active-source` | `target-info` | `epoch` | Meaning | Go to |
|---|---|---|---|---|
| present | present | present | Full conjunction — healthy, transmitting. Nothing to recover. | — |
| **absent** | present | present | Quiesced-but-not-selected: the target pod is alive and post-dates the last quiesce (it already republished fresh `target-info`/`epoch`), only `active-source` is missing — e.g. PHASE 1 ran but PHASE 3 never landed, or a verified rollback restored nothing because none was set. | **Rung 1** |
| present | **absent** | **absent** | Stale hand-restore risk: `active-source` was set (by hand or by a switch) but the target pod predates it and never republished — the conjunction is still incomplete, supervisors stay parked, preview stays dead. This is Aftermath A's exact shape (#311) — **do not stop at re-patching `active-source` alone; it will not recover on its own.** | **Rung 2** |
| **absent** | **absent** | **absent** | All three cleared: quiesce ran (or the target pod never ran since the last quiesce) and nothing has republished since. This is the 2026-07-30 freeze shape (#327). | **Rung 2** |
| any other partial combination (e.g. only `epoch` absent) | | | Non-canonical — the switch play only ever produces the three rows above; a partial state outside them means something else mutated the ConfigMap directly, or you caught it mid-write. The conjunction gate is fail-closed regardless of which key is missing, so treat as unhealthy. | **Rung 2** |

### Rung 1 — one-key coordinator patch

**Valid only when Rung 0 showed `target-info` AND `epoch` both present** —
i.e. the pod that must republish them is already alive and already did.
This is the fast path: it recovers in seconds because it does not wait on
any pod to (re)start.

```bash
kubectl -n mxl patch cm mxl-coordinator --type merge \
  -p '{"data":{"active-source":"<source-id>"}}'
```

`<source-id>` is whatever source you are restoring transmission to — the
value the switch's own PHASE 3 would have set
(`topology_params.viewer.source_selection` on the receiver, projected by
`switch_validate.yml` — the `l3_topology_viewer_projection` filter — into
`_switch_target_projection.active_source`). If you do not already know it,
the safest source is the coordinator's own pre-incident state (a prior
`kubectl get cm ... -o jsonpath` capture, an AWX job's extra_vars for the
switch that last ran, or the NetBox catalog record for the intended
source) — never guess.

Confirm the patch actually landed and the conjunction is complete:

```bash
kubectl -n mxl get cm mxl-coordinator -o jsonpath='{.data}'
```

Good result: all three keys present, `active-source` equal to what you set.
Bad result: `active-source` set but the viewer/preview still dead — that
means `target-info`/`epoch` were *not* actually fresh (re-check Rung 0's
table; you may have misread a transitional state) — drop to **Rung 2**.

This is Aftermath B's exact recovery (#311): a fresh target pod had already
republished `target-info`+`epoch`, so the one-key `active-source` patch
alone fully recovered the preview in seconds.

### Rung 2 — rollout restart the view target, then patch

**Use when Rung 0 showed `target-info` and/or `epoch` absent** — the
target pod either predates the incident or never ran since. There is no
key to hand-write for either; the only way to mint fresh values is to make
the target pod start.

```bash
kubectl -n mxl rollout restart deployment/<receiver_instance>-target
kubectl -n mxl rollout status deployment/<receiver_instance>-target --timeout=120s
```

(`<receiver_instance>` is the viewer's Helm release name, e.g.
`mxl-videotest-view` — the Deployment the switch play itself polls is
literally `<receiver_instance>-target`, see
`roles/l3_run_guard/tasks/switch_poll_upgrade_ready.yml`.)

Once the rollout reports success, re-check the coordinator — the fresh
pod's publisher should have republished `target-info`/`epoch` on startup:

```bash
kubectl -n mxl get cm mxl-coordinator -o jsonpath='{.data}'
```

Good result: `target-info`/`epoch` now both present (fresh, different
values from before the restart). Proceed to the **Rung 1** patch to set
`active-source`. Bad result: still absent after the rollout genuinely
completed — the publisher itself did not fire; do not keep retrying the
restart, escalate to **Rung 3**.

**Field-validated 2026-07-30 (#327):** all three coordinator keys were
absent (the target pod predated the quiesce, so it had never republished);
`rollout restart` of the target Deployment produced a fresh pod that
republished `epoch`+`target-info`, and the follow-up one-key
`active-source` patch fully recovered the preview. Frozen-to-picture in
under 3 minutes.

**What Rung 2 does NOT fix:** it only forces a fresh pod to publish. If the
underlying reason `target-info`/`epoch` never appeared is something other
than "the pod is stale" (chart drift, a broken publisher, a coordinator
namespace/name mismatch), the restart will complete but the keys will stay
absent — that is the signal to stop and go to Rung 3, not to keep
restarting.

### Rung 3 — full redeploy

**Use when Rung 2's restart completes but the conjunction still does not
recover**, or when you already know the topology itself (not just the
target pod) is broken.

A full redeploy exercises the launcher's install path end-to-end, which is
squarely orchestration-backend territory (a catalog launch via whichever
backend currently deployed this workload — AWX-driven, or a direct
`run-playbook.sh` invocation per ADR-0010) — this cluster-truth runbook
does not prescribe which surface to drive it from.

**Node-saturation caveat (#311):** before tearing down or redeploying, check
whether the existing pipeline is still generating even though it is parked
(supervisors that never un-park can still be running, just not
transmitting to anyone). A parked-but-generating pipeline can saturate the
node badly enough to break the ingress warmup gate the deploy waits on —
the exact trap #311 hit. If the node looks saturated, scale the `mxl`
namespace's deployments to 0 first:

```bash
kubectl -n mxl get deploy -o name
kubectl -n mxl scale <deployment> --replicas=0
```

Confirm the scale-down actually freed capacity (`kubectl top nodes`, or
whatever monitoring surface is available) before starting the redeploy —
scaling to 0 and immediately redeploying into a node that has not yet
released the CPU it was holding just moves the saturation window instead
of avoiding it.

## The facility lock

The facility lock is a single ConfigMap
(`roles/l3_run_guard/defaults/main.yml`: `l3_lock_namespace: nmos`,
`l3_lock_configmap_name: dmf-facility-lock`) that every L3 launcher/switch
entry point acquires before mutating the facility, and releases on every
terminal path. TTL is `l3_lock_ttl_seconds: 3600` (one hour).

### Inspect it

```bash
kubectl -n nmos get cm dmf-facility-lock -o jsonpath='{.data}'
```

Fields (`roles/l3_run_guard/tasks/lock.yml`): `run_id`, `holder_attempt_id`,
`holder` (the AWX job id, or the literal `direct` for an off-cluster
`run-playbook.sh` run), `created_at` (unix epoch seconds), `ttl_seconds`.
If the `get` errors `NotFound`, no run currently holds the facility —
nothing to do here.

Compute the lock's age against the TTL:

```bash
kubectl -n nmos get cm dmf-facility-lock -o jsonpath='{.data.created_at}'
date +%s
```

Age = `date +%s` output minus `created_at`. Compare against `ttl_seconds`
(3600 by default).

### Why a lock can be sitting there at all: it leaks by design

`lock_release.yml` runs on every normal completion path — success
(`launch_success.yml`, and the switch play's own success step at
`playbooks/switch-mxl-fabrics-demo.yml:387-393`, both calling it from
their own in-`block:` steps, before `rescue:` ever starts), refusal
(`_refuse_pre_mutation.yml`), and rollback-terminal
(`rollback_terminal.yml`, `rollback.yml`) — plus a universal
`rescue:`/`always:` backstop (`roles/l3_run_guard/tasks/release.yml`,
included from every launch/teardown/rollback playbook's outer block) that
catches anything the on-path calls miss. **A play that is killed mid-run
(SIGKILL, node eviction, pod cycle) reaches none of these — on-path or
backstop — and therefore never releases the lock it holds.** This is not a
bug to work around — it is the documented tradeoff: the TTL is the
backstop for that failure mode, not the release path.

### When to WAIT

If the age is **within** `ttl_seconds`, the hold is genuine and the
refusal is **never override-able** by design
(`roles/l3_run_guard/tasks/_lock_acquire_one_attempt.yml`,
`facility-busy` refusal) — no manual action recovers this faster. Either
the run holding it finishes and releases normally, or the TTL expires and
the **next** run to attempt acquisition reclaims it automatically — no
operator action needed at all. The reclaim path
(`_lock_acquire_one_attempt.yml`'s `action == 'reclaim'` branch) does a
preconditioned delete of the stale lock and re-creates it for the new
attempt, verified against a fresh cluster read before it trusts its own
acquisition.

**Field-validated 2026-07-31 (#327):** a lock left by a 2026-07-30
mid-run kill (holder `497`) was still present when the next switch attempt
(the b→a switch-back acceptance) ran; the switch guard's own TTL-expired
takeover claimed over it automatically, exactly as designed — no manual
delete was needed or performed.

### When to DELETE it manually

Only after you have **verified no live run holds it**. The lock's own
`created_at`/`ttl_seconds` already tells you the safe case (age > TTL —
just wait, the next run reclaims it, see above). A manual delete is for
the case where you need the facility free **before** TTL expiry and you
have independently confirmed nothing is actually running:

- Cross-reference `holder` against the orchestration backend's own job
  status (an AWX job id that shows `failed`/`canceled`/`error`, not
  `running`; or, for `holder: direct`, confirm the operator's
  `run-playbook.sh` process is not still attached to a terminal anywhere).
- Only once that is confirmed:

  ```bash
  kubectl -n nmos delete cm dmf-facility-lock
  ```

This bypasses the precondition-safe delete the role itself always uses
(`lock_release.yml`'s delete carries the lock's own `metadata.uid` as a
delete precondition specifically so a concurrent legitimate holder is never
destroyed out from under itself) — a bare `kubectl delete` has no such
guard. That is exactly why the "verify no live run holds it" step is not
optional: deleting a lock a live run genuinely still holds lets a second
run start against the same facility concurrently, which the lock exists to
prevent.

## The failed-rollback dead-end

Separate from the coordinator/lock recovery above: a **failed rollback**
can leave the facility permanently blocked through a circular set of
refusals, with no sanctioned way out today. Observed live 2026-07-29
(umbrella issue
[#311](https://github.com/dmfdeploy/dmfdeploy/issues/311)):

1. A deploy operation errors into `failed_rollback_required`, auto-rollback
   triggers.
2. The auto-rollback itself **fails**. The original operation stays stuck
   in `failed_rollback_required`.
3. Catalog teardown → `409 facility-busy`, naming that stuck operation as
   the blocker.
4. A manual rollback retry is accepted and runs, but it creates a **new**
   operation and reaches `rollback_incomplete` — it does **not** clear the
   original stuck operation.
5. Teardown retried → still `409 facility-busy`, same blocking operation.
6. A direct workflow-template launch (bypassing the catalog endpoint) is
   refused `409 use-catalog-endpoint` — the console's own policy redirects
   back to the very catalog endpoint that is blocked.

Every recovery route is either blocked by, or routes back into, the same
stuck operation. **Circular.**

**Workaround found, and why it is not a fix:** restarting the console
(`dmf-cms`) clears its **in-memory** operation store, which releases the
advisory block and makes the normal catalog teardown reachable again. This
works, but:

- it discards operation history for every other operation, not just the
  stuck one;
- it is not discoverable — nothing in the `409` response tells an operator
  this is the way out;
- it treats what should be a persistent lifecycle state as if it were a
  cache.

**This gap is OPEN.** There is no sanctioned, in-console clear-path for a
failed rollback today; the console restart above is a workaround an
operator can reach for, not a fix. Track resolution on umbrella issue
[#311](https://github.com/dmfdeploy/dmfdeploy/issues/311) — do not treat
this runbook as closing it.

## Field validations

| Date | Issue | What was validated |
|---|---|---|
| 2026-07-30 | [#327](https://github.com/dmfdeploy/dmfdeploy/issues/327) | Freeze recovery: all three coordinator keys absent → `rollout restart` of the target Deployment → fresh pod republished `epoch`+`target-info` → one-key `active-source` patch → preview recovered, frozen-to-picture in under 3 minutes. Validates Rung 2 → Rung 1 of this ladder. |
| 2026-07-31 | [#327](https://github.com/dmfdeploy/dmfdeploy/issues/327) | Facility-lock TTL takeover: a lock left by a 2026-07-30 mid-run kill (stale holder) was still present at the next switch attempt; the switch guard's own TTL-expired reclaim claimed over it automatically during the b→a switch-back acceptance, with no manual lock deletion. Validates the "wait for TTL expiry" guidance above. |

Both are live-cluster observations against a rotating test-env id (not
reproduced here — see the linked issue threads for the operator-local
detail). The mechanisms they validate — the coordinator conjunction gate
and the lock's TTL-reclaim path — are read directly from
`playbooks/switch-mxl-fabrics-demo.yml` and `roles/l3_run_guard/tasks/`
elsewhere in this repo, not from the incident reports alone.
