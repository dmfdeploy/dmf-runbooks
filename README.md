# dmf-runbooks

[![CI](https://github.com/dmfdeploy/dmf-runbooks/actions/workflows/ci.yml/badge.svg)](https://github.com/dmfdeploy/dmf-runbooks/actions/workflows/ci.yml)

Ansible playbooks that launch, change, tear down and roll back the media functions of the dmfdeploy platform catalog.

Part of [dmfdeploy](https://github.com/dmfdeploy/dmfdeploy), an independent open prototype that explores the ideas of the EBU Dynamic Media Facility on commodity infrastructure. Status: prototype, version 0.4.9.

> **New to the project vocabulary?** See the [glossary](https://github.com/dmfdeploy/dmfdeploy/blob/main/docs/GLOSSARY.md) for project-coined terms such as umbrella and component repo.

Each playbook targets `localhost`, and AWX runs them inside its execution environment pod. An operator can also run `rollback-run.yml` directly from a workstation with `bin/run-playbook.sh` in the `dmf-env` repository, the entry point that [ADR-0010](https://github.com/dmfdeploy/dmfdeploy/blob/main/docs/decisions/0010-run-playbook-as-sanctioned-entry.md) sanctions. The launch, switch and teardown playbooks install, change or remove a function's Helm chart, through the `kubernetes.core.helm` module or the `helm` command, and record the result in NetBox through a per-function role. The finalise and rollback playbooks clean up after those runs, as described under [Structure](#structure). The Helm charts live in the sibling `dmf-media` repository.

The playbooks do more than start a chart. Before any change, every playbook takes a facility-wide lock through the `l3_run_guard` role. The launch playbooks also save a snapshot of the starting state and check that the new workload fits the node's resource budget. If a launch fails after it has changed something, the guard marks the run for rollback, and `rollback-run.yml` restores the saved state.

## Structure

- `playbooks/` holds the catalog playbooks listed below.
- `roles/l3_run_guard/` holds the run guard. Every catalog playbook includes it to check who is asking, take the shared facility lock and release the lock when the run ends. The launch, switch, purge and rollback playbooks also report a structured outcome. A teardown reports one only when it fails, and on success the AWX job result is the signal. Only the launch playbooks use its full gate, which also saves the pre-run snapshot and checks the resource budget. Teardown and purge take the lock only, and the switch playbook keeps its own baseline for a Helm rollback.
- `roles/nmos-cpp/`, `roles/nmos-crosspoint/` and `roles/mxl/` hold the NetBox tasks for one function each: provision, configure and finalise.
- `roles/netbox_catalog_common/` holds NetBox tasks that several functions share, such as workload tags and the permanent purge of leftover records.
- `tests/` holds the Ansible test playbooks for the guard and the launchers.
- `docs/runbooks/` holds operational runbooks.

## Catalog playbooks

Launch:

- `launch-nmos-cpp.yml` installs the NMOS IS-04 and IS-05 registry chart and moves the NetBox service to its running state.
- `launch-nmos-crosspoint.yml` installs the NMOS crosspoint chart, which subscribes to the IS-04 registry, and updates NetBox.
- `launch-mxl-fabrics-demo.yml` installs the MXL fabrics demo chart as a source or a viewer, and updates NetBox.

Switch:

- `switch-mxl-fabrics-demo.yml` points a running MXL viewer at a different source. It updates the coordinator ConfigMap and the viewer's Helm release, and it verifies a rollback if the change fails.

Teardown:

- `teardown-nmos-cpp.yml` removes the NMOS registry Helm release and sets the NetBox tag back to bootstrapped.
- `teardown-nmos-crosspoint.yml` removes the crosspoint Helm release and sets the NetBox tag back to bootstrapped.
- `teardown-mxl-fabrics-demo.yml` removes an MXL source or viewer release and finalises its NetBox records.

Finalise and rollback:

- `finalise-purge.yml` deletes the NetBox records that remain after a workload is fully finalised. It changes NetBox only and uses its own delete-only token.
- `rollback-run.yml` rolls back a run that started changing the cluster and then failed. It uses the run's saved snapshot.

For the architecture decisions behind this layout, see ADR-0014 (multi-project AWX layout) and ADR-0025 (execution environment as runtime, with Helm) in the [dmfdeploy decision record](https://github.com/dmfdeploy/dmfdeploy/tree/main/docs/decisions).

## Operational runbooks

- [Media-Path Recovery via Cluster Truth](docs/runbooks/media-path-recovery.md): decision ladder for recovering a stuck/frozen MXL media path (coordinator ConfigMap + facility lock) by reading and writing cluster truth directly with `kubectl`, independent of the orchestration backend.

## License

This project is licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for the full text. Third-party components (notably Sony nmos-cpp) are listed in [NOTICE](NOTICE).

## Repository layout notes

`AGENTS.md`, `CLAUDE.md` and `QWEN.md` hold instructions for AI coding agents that help maintain this repository. You do not need them to use or contribute to the code.
