# DMF Runbooks

Thin launcher playbooks for DMF Platform catalog entries.

> **2026-05-23 — ADR-0025 Lane B landed.** Catalog launchers now run with
> `connection: local` inside a custom AWX EE pod + `kubernetes.core.helm`
> against charts in the sibling `dmf-media` repo. SSH-to-control-node
> (ADR-0016 Path A) is retained for 693-class infrastructure plays only.

These playbooks run in AWX as thin wrappers around a Helm install and
the per-function NetBox catalog operations. Each launcher targets
`localhost` (the AWX EE pod), invokes the function's chart with
`kubernetes.core.helm`, and calls the function's `nmos-cpp`-style role
for NetBox registration + lifecycle tag flips.

## Structure

- `playbooks/` — thin launcher playbooks for catalog entries (launch, teardown)
- `roles/<function>/` — NetBox-side launcher tasks per function (provision, configure-tag-flip, finalise)
- Helm charts for each function live in `dmf-media/charts/<function>/`

## Catalog Entries

- `launch-nmos-cpp.yml` — Launch NMOS IS-04/05 registry + mock nodes (provision NetBox entry → helm install → tag flip)
- `teardown-nmos-cpp.yml` — Teardown NMOS workloads (helm uninstall → tag finalise)

See DMF Platform ADR-0014 for the multi-project AWX layout and ADR-0025 for the EE-as-runtime + Helm execution model (both in the DMF Platform umbrella repo).

## License

This project is licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for the full text. Third-party components (notably Sony nmos-cpp) are listed in [NOTICE](NOTICE).
