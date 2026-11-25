# Contributing to dmf-runbooks

Thin AWX launcher playbooks + NetBox-side catalog roles for the dmfdeploy platform.

This repo is part of the **dmfdeploy platform**. GitHub is the canonical home and the
single source of truth: all changes land via **Pull Request** against `main`.

## Quick start

1. Read the platform overview in the [dmfdeploy/dmfdeploy](https://github.com/dmfdeploy/dmfdeploy) umbrella repo
   (`docs/architecture/dmfdeploy-platform-plan.md`) and apply any relevant ADRs from
   `docs/decisions/INDEX.md`.
2. Set up your machine and run the checks (see [Develop and test](#develop-and-test)).
3. Fork or branch, make your change on a topic branch, open a PR against `main`.
4. Ensure CI is green, your commits are **signed off** (see DCO below), and the
   PR body links its issue (see [What a pull request needs](#what-a-pull-request-needs)).

## Develop and test

This section is the one place for development and test commands. The README,
`AGENTS.md` and `tests/harness/README.md` point here. The commands match what CI
runs in `.github/workflows/ci.yml`. Where CI doesn't run a command, the text
says so.

### Prerequisites

- **Python 3.12 or later.** CI runs one test leg on 3.12 and one on 3.11. See
  [Run the pinned CI leg](#run-the-pinned-ci-leg).
- **Network access to Ansible Galaxy**, to install the collections.
- **Optional tools.** [gitleaks](https://github.com/gitleaks/gitleaks) powers
  the commit hook.

You don't need a cluster, NetBox, AWX, or a cloud account. The test playbooks
start stub Kubernetes and NetBox servers on your machine, and a stub `helm`
command stands in for Helm, so you don't need Helm installed either.

### Set up

Run these commands from the repository root. Check first that `python3` is
version 3.12 or later. If it isn't, use the full name of a newer interpreter,
such as `python3.13`, in place of `python3`.

```bash
python3 --version
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ansible-core ansible-lint yamllint kubernetes
ansible-galaxy collection install -r requirements.yml
```

`pip` installs the newest `ansible-core` that your Python supports. CI's
`latest` leg does the same on Python 3.12, so you may test a newer
`ansible-core` than CI does. The `kubernetes` package is the Python client that
the `kubernetes.core` modules need when a test runs them. `ansible-galaxy`
installs the collections into `~/.ansible/collections`, outside the virtual
environment, where other projects can see them.

### Run the fast checks

Run these commands from the repository root, with the virtual environment
active, before you push. They take about a minute together.

```bash
yamllint playbooks/ roles/
ansible-lint playbooks/ roles/ -p
for pb in playbooks/*.yml; do
  echo "=== syntax-check: $pb ==="
  ansible-playbook "$pb" --syntax-check
done
ansible-playbook tests/tag-preservation.yml
ansible-playbook tests/l3-budget-logic.yml
ansible-playbook tests/l3-budget-self-demand.yml
ansible-playbook tests/l3-playbook-structure.yml
ansible-playbook tests/l3-reserved-var-meta-test.yml
ansible-playbook tests/l3-topology-logic.yml
ansible-playbook tests/service-tag-patch-verified.yml
ansible-playbook tests/netbox-service-id-check.yml
```

| Command | Time |
| --- | --- |
| `yamllint` | A few seconds |
| `ansible-lint` | About 20 seconds |
| Syntax check of every playbook | A few seconds |
| `tests/tag-preservation.yml` | A few seconds |
| The five fast `tests/l3-*` suites | A few seconds each |
| `tests/service-tag-patch-verified.yml` | About 15 seconds |
| `tests/netbox-service-id-check.yml` | About 20 seconds |

### Run the slow suites

Five suites run a real playbook as a nested `ansible-playbook` process against
the stub servers, and each starts fresh stubs for every scenario. They are the
slow part of the test run. The times come from one run on an 8-core laptop.

```bash
ansible-playbook tests/l3-control-flow.yml
ansible-playbook tests/l3-topology-execution.yml
ansible-playbook tests/l3-switch-execution.yml
ansible-playbook tests/l3-finalise-purge-execution.yml
ansible-playbook tests/l3-playbook-execution.yml
```

| Suite | What it runs | Time |
| --- | --- | --- |
| `l3-control-flow` | The run guard's task files, its lock retry loop and its refused inputs | About 2 minutes |
| `l3-topology-execution` | The MXL launch and teardown playbooks with a topology | About 3 minutes |
| `l3-switch-execution` | `switch-mxl-fabrics-demo.yml` | About 4.5 minutes |
| `l3-finalise-purge-execution` | `finalise-purge.yml` | About 6.5 minutes |
| `l3-playbook-execution` | Every catalog playbook, with a fault injected | About 14 minutes |

All five together take about 30 minutes. Before you push, run the fast checks
and the slow suite that matches the playbook you changed. If you change
`roles/l3_run_guard/`, which every playbook uses, run all five, or push and let
CI run them. If you run a suite under a time limit, allow at least 20 minutes
for `l3-playbook-execution`.

### Check the test harness

The stub servers and the stub `helm` live in `tests/harness/`. Its own
self-check starts both servers and drives each one. CI doesn't run it. Run it
when you change the harness. It takes a few seconds.

```bash
ansible-playbook tests/harness/_smoke_test.yml
```

### What CI runs

The `ci` workflow in `.github/workflows/ci.yml` has these jobs:

- **`ansible`** installs the newest `ansible` package, then runs `yamllint`,
  `ansible-lint`, the syntax check and `tests/tag-preservation.yml` from the fast
  checks.
- **`l3-harness`** installs `ansible-core` and the `kubernetes` package, then
  runs the `l3-*` suites, `service-tag-patch-verified` and
  `netbox-service-id-check` from the two lists in this section, twice. The `ee`
  leg uses the AWX execution environment's Python (3.14), `ansible-core` and
  `kubernetes`. It reads the two pins from dmf-infra at the commit in
  `EE_INFRA_REF` in the workflow, and a step fails if the interpreter or
  `ansible-core` differs from them. The `latest` leg uses Python 3.12 and the
  newest of both packages. The job allows 60 minutes.
- **`review-comments`** runs `bin/check-review-comments.sh` and its
  `--self-test`.
- **`working-model`** confirms that the working-model block in `AGENTS.md`,
  `CLAUDE.md` and `QWEN.md` matches the umbrella template. It needs a checkout
  of the umbrella repo, so it runs in CI only.

The `guard` workflow scans for secrets and vulnerabilities, lints the workflow
files, and checks commit subjects and the issue link. The `DCO` workflow checks
sign-offs. Both run in CI only. None of these checks needs a live environment.

You can run the review-comment check yourself:

```bash
bin/check-review-comments.sh
bin/check-review-comments.sh --self-test
```

### Run the pinned CI leg

A green local run on a new `ansible-core` doesn't cover the `ee` leg. Behavior
has differed between the two in the past. To reproduce the leg, make a second
virtual environment in a directory named `.venv-ee`, which leaves your first one
in place. Use a Python 3.14 interpreter in place of `python3` for the first two
commands, and the pins from dmf-infra at the `EE_INFRA_REF` commit
(`k3s-lab-bootstrap/requirements-ansible-core.txt` and the `kubernetes==` line of
`k3s-lab-bootstrap/ee/requirements.txt`).

```bash
python3 --version
python3 -m venv .venv-ee
. .venv-ee/bin/activate
python -m pip install "ansible-core==<the pin>" "kubernetes==<the pin>"
ansible-galaxy collection install -r requirements.yml
```

Then run the `tests/` commands from the fast and slow suite sections in this
environment, and check the interpreter and core with
`python tests/scripts/assert_toolchain.py --python 3.14 --core ansible-core==<the pin>`.
The `ee` leg doesn't run `yamllint` or `ansible-lint`, so you don't need to
install them here.

### Install the hooks

Run this once in each clone:

```bash
bin/install-hooks.sh
```

The script sets `core.hooksPath` to `.githooks/`. The `pre-commit` and
`pre-push` hooks scan your changes with gitleaks. They skip the scan with a
warning when gitleaks isn't installed. CI runs the same scan.

## Branch & PR model

- **GitHub Pull Requests only.** Direct push to `main` is blocked; force-push is
  banned; linear history is required.
- Topic branches: **`<handle>/<short-slug>`** (e.g. `jdoe/fix-probe-path`). One
  logical change per branch; rebase onto `main` rather than long-lived branches.
- A reviewer (per `CODEOWNERS`) and green required checks are needed to merge.

## What a pull request needs

CI checks each of these.

- **An issue link in the PR body.** Write a fully qualified reference to an issue
  in the [dmfdeploy/dmfdeploy](https://github.com/dmfdeploy/dmfdeploy/issues)
  backlog, such as `Refs dmfdeploy/dmfdeploy#912`. Use `Closes dmfdeploy/dmfdeploy#N`
  if the pull request finishes the issue. A bare `#N` doesn't count, because it
  points at this repo. If your change has no issue, such as a typo fix, a
  maintainer can add the `no-issue` label. Only maintainers can add labels, so
  if you open the pull request from a fork, ask in the PR. The check runs when
  you open the pull request, push a commit, or edit the PR body. Adding a label
  doesn't run it, so after a maintainer adds `no-issue`, edit the PR body or
  push a commit to run the check again.
- **A DCO sign-off on every commit.** Use `git commit -s`. See the next section.
- **Conventional commit subjects.** Use `type(scope): summary`, with one
  optional scope. The scope may use only `a-z`, `0-9`, `.`, `_` and `-`. The types are `feat`, `fix`, `docs`, `chore`, `refactor`, `test`,
  `build`, `ci`, `perf`, `revert` and `style`. An `!` before the colon marks a
  breaking change. Other types, such as `wip`, are rejected, and so is a scope
  list such as `(api,ui)`.
- **Passing CI.** The `ci` workflow runs the checks in
  [Develop and test](#develop-and-test). The `guard` workflow adds secret and
  vulnerability scans.

Also leave agent session links out of commit messages and the PR body. The
`guard` workflow rejects one known session-link format. It can't recognize
every tool's links, so reviewers look for them too.

## Developer Certificate of Origin (DCO)

We use the [Developer Certificate of Origin](https://developercertificate.org/),
not a CLA. **Every commit must be signed off:**

```bash
git commit -s -m "fix: correct the probe path"
```

This appends a `Signed-off-by: Your Name <you@example.com>` trailer certifying you
have the right to submit the work under the project license. A **DCO check** runs
on every PR and fails if any commit is missing the trailer. Amend with
`git commit --amend -s` or rebase with `git rebase --signoff main` to fix. PRs are
**rebase-merged** by default so your signed-off commits land on `main` unchanged.

## Versioning & releases

This repo carries a `VERSION` file (single semver line). Per **ADR-0005**,
`VERSION` is the single source of truth — any release-tagged change must update it
in the same commit. **No VERSION bump → no release.** Release tags are `v<VERSION>`,
created by release automation, never by hand.

## Secrets & public-safety posture

**Secrets stay in OpenBao.** Never commit, track, or reference credentials, tokens,
keys, kubeconfigs, or Terraform state — not even with a "remove later" TODO. Use
**placeholder syntax** for any IPs, DNS names, or operator identity in code, docs,
PR descriptions, or issues (`<control-node-public-ip>`, `dmf.example.com`,
`<handle>`). A local pre-commit gitleaks hook runs on commit, and CI runs
secret-scanning + scrub gates on every PR — but redaction is your responsibility
first. If you need a secret, ask a maintainer — do not improvise a transport.

## Must / Must not

### MUST
- Open changes as **GitHub PRs against `main`** with **signed-off** commits.
- Use Conventional Commit messages and `<handle>/<short-slug>` topic branches.
- Update `VERSION` in the same commit as any release-tagged change.
- Use **placeholder syntax** for all IPs / DNS / operator identity in every artifact.
- Keep launchers thin (logic in roles/charts, ADR-0014/0025); update `NOTICE` if a
  new upstream-derived role is added.

### MUST NOT
- Commit secrets, tokens, keys, kubeconfigs, or Terraform state.
- Push directly to `main`, force-push, or use `--no-verify` / `--no-gpg-sign`.
- Paste secrets, real IPs/DNS, or operator identity into issues, PRs, or CI logs.
- Hardcode AWX inventory IDs / cluster names.

## AI agent contract

Much of this platform is built by AI agents. Agents contribute the same way:
**PRs against `main`, signed off, CI green.** Additionally, agents must run cluster
mutation only via `bin/run-playbook.sh` (ADR-0010), must not use
`--no-verify`/`--force`/`--no-gpg-sign`, and must stop and ask before modifying a
sub-repo with uncommitted state.

## Reporting security issues

See [`SECURITY.md`](SECURITY.md). **Do not** open a public issue for a vulnerability.

## License & spec

Contributions are licensed under [Apache 2.0](LICENSE). The canonical governance
model is **ADR-0041 — DMF Release and Contribution Model** in the
[dmfdeploy/dmfdeploy](https://github.com/dmfdeploy/dmfdeploy)
umbrella repo (`docs/decisions/`).
