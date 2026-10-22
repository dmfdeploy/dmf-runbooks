# AGENTS.md — dmf-runbooks

This repo holds the thin AWX launcher playbooks for the dmfdeploy platform's
catalog entries (`playbooks/`, ADR-0014 multi-project AWX layout) and the
NetBox-side catalog roles (`roles/`).
Launchers run `connection: local` in an AWX EE pod and install Helm charts from
`dmf-media` (ADR-0025). Cross-repo state lives in the
[umbrella repo](https://github.com/dmfdeploy/dmfdeploy).

## Boot steps

1. In the umbrella repo, run `git pull` and `bin/generate-status.sh`, then read
   `STATUS.digest.local.md`. Post shared notes to the pinned issue
   [#618](https://github.com/dmfdeploy/dmfdeploy/issues/618).
2. Apply any fitting ADR from the umbrella's `docs/decisions/INDEX.md`. Run
   `bin/install-hooks.sh` once per clone.

## Build and test

After `ansible-galaxy collection install -r requirements.yml`, run what CI runs:

- `yamllint playbooks/ roles/` and `ansible-lint playbooks/ roles/ -p`
- `ansible-playbook <file> --syntax-check` for each file in `playbooks/`
- `ansible-playbook tests/tag-preservation.yml` and each `tests/l3-*.yml`

## Rules that are easy to miss

- Keep launchers thin. The chart owns workload manifests; `roles/<key>/` holds
  only NetBox registration and lifecycle tag-flip tasks.
- ADR-0016 (SSH to the control node) applies only to AWX-to-infrastructure
  plays (693-class), not to catalog launchers.
- Use Conventional Commit prefixes and `git commit -s`. Open a pull request
  against `main`; direct pushes are blocked.
- A release-tagged change updates `VERSION` in the same commit (ADR-0005).
  Never create release tags by hand.
- Never commit secrets, kubeconfigs or Terraform state. Use placeholders such
  as `<control-node-public-ip>` and `dmf.example.com`.

<!-- WORKING-MODEL-BLOCK-START — generated from umbrella docs/templates/working-model-block.md; do not edit copies, edit the template and run bin/check-working-model-sync.sh -->
## Working model (mandatory)

Canonical: [docs/WORKING-MODEL.md](https://github.com/dmfdeploy/dmfdeploy/blob/main/docs/WORKING-MODEL.md)
in the umbrella repo. The three rules that matter mid-task:

1. **Work starts at an issue** in the canonical backlog
   ([dmfdeploy/dmfdeploy issues](https://github.com/dmfdeploy/dmfdeploy/issues)):
   `component:*`/`workstream:*` labels **always**, plus a milestone **only if
   the work is scheduled** — unscheduled work gets `platform-debt` and no
   milestone (§2). Non-trivial work gets a plan doc in umbrella `docs/plans/`
   with `tracking_issue` frontmatter.
2. **The completing PR auto-closes its issue; you still flip the plan
   frontmatter by hand in that PR.** Reference umbrella issues **fully
   qualified** — `Closes dmfdeploy/dmfdeploy#N` (bare `#N` targets the wrong
   repo); the daily issue-close reconciler honors that ref, cross-repo
   included. Manual close is a fallback.
3. **Never invent a local backlog** (TODO files, ad-hoc trackers). Issues =
   liveness; plan frontmatter = design state; ADRs = decisions (RFC in
   Discussions first); STATUS.md = pointer file (operator notes live in pinned issue dmfdeploy/dmfdeploy#618);
   STATUS.local.md = live repo snapshot.
<!-- WORKING-MODEL-BLOCK-END -->
