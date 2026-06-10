# dmf-runbooks

Thin launcher playbooks for DMF Platform catalog entries.

## DMF Platform context — read first

This repo is a component of the **DMF Platform**, an umbrella workspace
checked out alongside this repo. Operators set `$DMFDEPLOY_UMBRELLA` to its
local path. Cross-cutting state (status, decisions, plans, skills) lives
there, not here.

Before any non-trivial change in this repo:

```bash
cd "$DMFDEPLOY_UMBRELLA"
git fetch && git pull
bin/generate-status.sh --no-fetch    # refreshes STATUS.md
```

Then read in order:
1. `dmfdeploy/STATUS.md` — what's happening across all repos right now
2. `dmfdeploy/CLAUDE.md` — full boot ritual + workspace map
3. `dmfdeploy/docs/decisions/INDEX.md` — ADRs applicable to your task
4. The most recent file under `dmfdeploy/docs/handoffs/`

---

Thin launcher playbooks for the DMF Platform's AWX multi-project catalog layout (ADR-0014).

## Catalog Entries

- `playbooks/launch-nmos-cpp.yml` — Launch NMOS IS-04/05 registry + mock nodes (configure stage)
- `playbooks/teardown-nmos-cpp.yml` — Teardown NMOS workloads (finalise stage)

## Architecture Notes

> **2026-05-23 — ADR-0025 Lane B landed.** Catalog launchers now execute
> in-cluster via the AWX EE pod + Helm chart. SSH-to-control-node (ADR-0016
> Path A) is retained for AWX → infrastructure plays (693-class) only.

**Current state (per ADR-0025):**
- Launcher playbooks target `localhost` with `connection: local`. The
  playbook runs inside a custom AWX EE pod hosted in cluster-internal Zot.
- The k8s workload is deployed via `kubernetes.core.helm` against the chart
  at `dmf-media/charts/nmos-cpp/` (chart pushed to Zot as an OCI artifact).
- The `nmos-cpp` role in this repo (`roles/nmos-cpp/`) is slimmed to NetBox
  registration + lifecycle tag-flip tasks only. The chart owns workload
  manifests.
- Future Layer 5 catalog roles (`ebu-list`, `flow-exporters`, etc.) follow
  the same pattern: chart in `dmf-media/charts/`, NetBox-side launcher in
  `dmf-runbooks/roles/<key>/`.

ADR-0014 (multi-project AWX layout) remains valid. ADR-0016 remains valid for
AWX → infrastructure plays (693-class) and is fully superseded by ADR-0025 for
media catalog launchers.
