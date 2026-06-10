# nmos-cpp

**Scope:** AMWA NMOS IS-04/05 registration & discovery service.

**EBU placement:** Layer 5 (Media Functions), Orchestration vertical.

**Authoritative location:** This repo (`dmf-runbooks`) since 2026-05-06.
The role previously also lived in `dmf-media/roles/nmos-cpp/`; that
copy was deleted to remove drift. The catalog-entry metadata
(`catalog/nmos-cpp.yaml`) remains in `dmf-media` as the catalog
source-of-truth.

## Lifecycle (ADR-0012)

> **2026-05-23 — ADR-0025 Lane B landed.** Per DMF Platform ADR-0025,
> this role slims down to **NetBox-side tasks only**. The k8s workload
> deployment lives in the Helm chart at `dmf-media/charts/nmos-cpp/`.
> The launcher invokes
> `kubernetes.core.helm` to install/upgrade the chart and calls this role
> only for NetBox registration + tag flips.

The role's `tasks/main.yml` dispatches to per-stage tasks based on
`nmos_stage`. The launcher playbooks wire AWX to those stages:

| Stage | Tasks file | AWX job | Behavior |
|---|---|---|---|
| Provision | `tasks/provision.yml` | (merged into launch) | Ensures NetBox tag taxonomy + `ipam.Service` (`lifecycle:bootstrapped`). **No workload deployed; no namespace/ConfigMap created here — the Helm chart owns those.** |
| Configure-tag-flip | `tasks/configure.yml` | `media-launch-nmos-cpp` (post-helm step) | Flips NetBox tag to `lifecycle:active`. Runs after the `kubernetes.core.helm` install step and explicit readiness gate in the launcher. |
| Finalise | `tasks/finalise.yml` | `media-finalise-nmos-cpp` (post-helm-uninstall step) | Flips NetBox tag back to `lifecycle:bootstrapped`. Runs after `kubernetes.core.helm` uninstall in the teardown launcher. |

The `media-launch-nmos-cpp` job sequence becomes: provision (NetBox HTTP)
→ helm install (chart from Zot) → tag flip (NetBox HTTP). Idempotent on
re-run.

**Execution model (current):** ADR-0025 EE-as-runtime. The launcher uses
`connection: local` inside a custom AWX EE pod hosted in cluster-internal
Zot. The chart at `dmf-media/charts/nmos-cpp/` is the canonical home for
the k8s manifests; this role no longer carries them.

**Historical note:** ADR-0016 Path A (SSH to the k3s control node) was the
media launcher execution model from 2026-05-06 until ADR-0025 Lane B landed
on 2026-05-23. It remains canonical for 693-class infrastructure plays only.

## Launchers

| Launcher (this repo) | AWX job template | Stage(s) called |
|---|---|---|
| `playbooks/launch-nmos-cpp.yml` | `media-launch-nmos-cpp` | provision + configure |
| `playbooks/teardown-nmos-cpp.yml` | `media-finalise-nmos-cpp` | finalise |

Both target `localhost` inside the AWX EE pod through the DMF catalog
Container Group. They do not use the `awx-control-node-ssh` Machine
credential.

## Files

| Path | Purpose |
|---|---|
| `files/Dockerfile.registry` | Multi-stage build for nmos-cpp-registry (Sony upstream) |
| `files/Dockerfile.node` | Multi-stage build for nmos-cpp-node (Sony upstream) |
| `tasks/main.yml` | Stage dispatcher (`nmos_stage` → provision/configure/finalise) |
| `tasks/provision.yml` | NetBox tag taxonomy + `ipam.Service` create |
| `tasks/configure.yml` | NetBox tag flip → active |
| `tasks/finalise.yml` | NetBox tag flip → bootstrapped |
| `defaults/main.yml` | Role defaults (namespace, images, logging, NetBox tag taxonomy) |

## Build (maintainer action; Docker-compatible build host)

The Dockerfiles require an explicit upstream Sony nmos-cpp SHA per
DMF Platform ADR-0025 and the public container registry publishing plan
(both in the DMF Platform umbrella repo, §5.1 of the latter).
Builds from upstream `master` fail loud — `NMOS_CPP_REF` is required, no default.

Pick a Sony nmos-cpp commit (release tag or specific SHA) and record it in
release notes:

```bash
export REPO_ROOT="$(git rev-parse --show-toplevel)"
export DOCKER_HOST="${DOCKER_HOST:-unix://$HOME/.colima/docker-build/docker.sock}"
export NMOS_CPP_REF=<full-sha-from-https://github.com/sony/nmos-cpp/commits/master>
export VCS_REF=$(git -C "$REPO_ROOT" rev-parse --short HEAD)
export IMAGE_VERSION=0.1.0

cd "$REPO_ROOT/roles/nmos-cpp/files/"

docker build \
  --build-arg NMOS_CPP_REF="$NMOS_CPP_REF" \
  --build-arg IMAGE_VERSION="$IMAGE_VERSION" \
  --build-arg VCS_REF="$VCS_REF" \
  -t registry.dmf.example.com/dmf/nmos-cpp-registry:$IMAGE_VERSION \
  -f Dockerfile.registry .

docker build \
  --build-arg NMOS_CPP_REF="$NMOS_CPP_REF" \
  --build-arg IMAGE_VERSION="$IMAGE_VERSION" \
  --build-arg VCS_REF="$VCS_REF" \
  -t registry.dmf.example.com/dmf/nmos-cpp-node:$IMAGE_VERSION \
  -f Dockerfile.node .
```

The resulting image embeds OCI labels and `/etc/dmf/nmos_cpp_ref` for
post-hoc audit (`docker run --rm <image> cat /etc/dmf/nmos_cpp_ref`).

## Publish — two registries (operator action; secrets via stdin only)

Per the `dmf-cluster-access` skill §0 and ADR-0007, secrets never flow
through an AI agent's transcript. Run these in your own terminal.

### Public publish to GHCR (`ghcr.io/dmfdeploy/*`)

Per the DMF Platform public container registry publishing plan
(in the umbrella repo). The script handles isolated DOCKER_CONFIG,
tag policy, and prints follow-up reminders:

```bash
# Interactive (paste GHCR token at prompt — won't echo):
roles/nmos-cpp/scripts/publish-to-ghcr.sh

# Or pipe from your password manager:
<password-manager-command> | \
  roles/nmos-cpp/scripts/publish-to-ghcr.sh
```

**Tag policy:**

- Current local images (built from upstream `master` before Dockerfile
  hardening): publish as `:0.1.0-dev`, **keep package private**. Default
  behavior of the script.
- Canonical images (built with `NMOS_CPP_REF` pinned): publish as
  `:0.1.0`, then make the package public in the GitHub Packages UI.
  Set `IMAGE_TAG=0.1.0` env var when invoking the script.

The first push creates a new GHCR package; operator must visit
`https://github.com/orgs/dmfdeploy/packages` to confirm and adjust
visibility.

### Cluster-internal publish to Zot (runtime mirror)

For deploys against a DMF environment, mirror images into in-cluster Zot via
the existing script:

```bash
roles/nmos-cpp/scripts/push-nmos-images.sh
```

This builds (unconditionally — see script's TODO if you want a build-skip
flag) and pushes to `registry.dmf.example.com` (env-specific Zot ingress).
Stage 4b of the convergence plan will later replace this with a
GHCR-digest-aware mirror (`600-zot-seed.yml` per the public registry plan
§6).

## References

- ADR-0012: Configure distinct from Provision (with historical 2026-05-06 Path A pivot note and 2026-05-19 terminology note on two configure-stage usages)
- ADR-0013: Media function catalog model
- ADR-0014: Multi-project AWX layout (this role lives in dmf-runbooks; chart lives in dmf-media; catalog entry in dmf-media)
- ADR-0016: Control-node SSH via cloud-init + OpenBao (Path A — retained for 693-class infrastructure plays only)
- **ADR-0025: Ansible runs in in-cluster pods using a Zot-hosted EE image; catalog functions deploy as Helm charts (Accepted 2026-05-23)**
- Pivot plan (parent; Lane B landed, Lane C in flight): `docs/plans/DMF Cluster-Internal Ansible Execution and Catalog Helm Pivot Plan 2026-05-19.md`
- Pivot plan (prior, historical): `docs/plans/Move 1 Gate 2 - Pivot to Path A for Catalog Launchers 2026-05-06.md`
- Catalog entry: `dmf-media/catalog/nmos-cpp.yaml`
- Helm chart (target state): `dmf-media/charts/nmos-cpp/`
