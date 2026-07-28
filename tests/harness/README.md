# l3_run_guard test harness

Freestanding test infrastructure that lets a **real** `ansible-playbook` run
exercise the **real, unmodified** `roles/l3_run_guard` task files end-to-end
against a **controlled backend**, so control-flow behavior (retry loops, lock
acquisition races, checkpoint fencing, snapshot collision handling) can be
proven by actual execution rather than by pure-function unit tests alone.

This directory is intentionally independent of `roles/l3_run_guard/` — it
does not import from or modify anything under `roles/l3_run_guard/tasks/` or
`roles/l3_run_guard/filter_plugins/`.

## What's here

| File | Purpose |
|---|---|
| `stub_k8s_api.py` | Stdlib-only HTTP server implementing the K8s REST surface (ConfigMap CRUD, Node/Namespace/Pod reads) the role's tasks call. |
| `stub_netbox_api.py` | Stdlib-only HTTP server implementing the NetBox `ipam.services` REST subset the role's tasks call. |
| `stub_helm.sh` | Bash script standing in for the `helm` CLI — handles `list`/`get values`/`rollback`/`uninstall`/`pull`/`template`. |
| `start_stubs.py` | CLI launcher: starts both stub servers as a detached background process, prints `K8S_STUB_URL=`/`NETBOX_STUB_URL=` lines, writes a pidfile. |
| `stop_stubs.py` | CLI companion: stops the detached process via its pidfile. |
| `_smoke_test.yml` | Self-verification playbook for this harness (not for the role — see below). |

No third-party Python dependencies anywhere (`http.server`, `json`, `uuid`,
`threading`, stdlib only) — everything here runs under a bare `python3` in an
Ansible execution environment with no pip installs.

## Starting the stubs from a test playbook

```yaml
pre_tasks:
  - name: Start the K8s + NetBox stub servers (detached)
    ansible.builtin.command:
      argv:
        - python3
        - "{{ playbook_dir }}/start_stubs.py"
        - --pidfile
        - /tmp/l3-harness.json
    register: _harness_start
    changed_when: false

  - name: Parse stub base URLs from start_stubs.py stdout
    ansible.builtin.set_fact:
      k8s_stub_url: "{{ _harness_start.stdout_lines | select('match', '^K8S_STUB_URL=') | first | regex_replace('^K8S_STUB_URL=', '') }}"
      netbox_stub_url: "{{ _harness_start.stdout_lines | select('match', '^NETBOX_STUB_URL=') | first | regex_replace('^NETBOX_STUB_URL=', '') }}"

post_tasks:
  - name: Stop the stub servers
    ansible.builtin.command:
      argv: [python3, "{{ playbook_dir }}/stop_stubs.py", --pidfile, /tmp/l3-harness.json]
    changed_when: false
```

`start_stubs.py` uses `port=0` internally (OS-assigned free port) so parallel
test runs never collide. It re-execs itself as a **detached subprocess**
(`subprocess.Popen(..., start_new_session=True)`) rather than `os.fork()`
after the server threads exist — forking after threads are already running
only carries the calling thread into the child process, silently dropping
the `ThreadingHTTPServer`'s `serve_forever` thread. The detached child writes
the pidfile once its servers are actually listening; the parent polls for it
(up to ~10s) before printing the URLs and exiting.

From a **plain shell** (not inside a playbook), the same script's stdout is
directly `eval`-able:

```bash
eval "$(python3 tests/harness/start_stubs.py --pidfile /tmp/l3-harness.json)"
echo "$K8S_STUB_URL $NETBOX_STUB_URL"
...
python3 tests/harness/stop_stubs.py --pidfile /tmp/l3-harness.json
```

## Pointing the role's extra_vars at the stubs

For a **direct/off-cluster** launch of `roles/l3_run_guard` (ADR-0010 path —
see `lock.yml`'s header comment), the role reads `l3_kube_api_url` /
`l3_kube_api_token` extra_vars directly and passes them straight through to
`ansible.builtin.uri` as `url`/`Authorization: Bearer` — no kubeconfig, no TLS
setup needed to point them at the stub:

```yaml
l3_kube_api_url: "{{ k8s_stub_url }}"        # e.g. http://127.0.0.1:54321
l3_kube_api_token: "fake-test-token"          # any non-empty string; the stub never validates it
l3_kube_api_validate_certs: false
netbox_api_url: "{{ netbox_stub_url }}"
netbox_api_token: "fake-netbox-token"
```

For the role's `kubernetes.core.k8s_info` / `kubernetes.core.k8s` calls
(which do NOT read `l3_kube_api_url`/`l3_kube_api_token` themselves — those
modules use the official `kubernetes` Python client's own connection
resolution), point them at the stub via the same module's own connection
params, either per-task:

```yaml
- kubernetes.core.k8s_info:
    api_version: v1
    kind: Node
    host: "{{ k8s_stub_url }}"
    api_key: fake-test-token
    validate_certs: false
```

or globally via the module defaults group / a generated kubeconfig file
(`KUBECONFIG` env var) if a test playbook needs to exercise the role's
unmodified tasks without adding per-task connection params — the stub imposes
no auth requirements beyond "some value is present," so a minimal kubeconfig
with a fake bearer token and `insecure-skip-tls-verify: true` pointed at
`k8s_stub_url` works too.

### Kubernetes-client-library quirks (found by iterating against a real run)

The official `kubernetes` Python client — which `kubernetes.core.k8s_info` /
`kubernetes.core.k8s` use internally via a `DynamicClient` — does **API
discovery** before issuing the actual request, even for something as simple
as listing Nodes. A bare JSON-parsing HTTP client (like
`ansible.builtin.uri`, which the role's raw REST calls use) never triggers
this, but `k8s_info`/`k8s` unconditionally do. The discovery sequence hit
against this stub was:

1. `GET /version` — a `VersionInfo`-shaped body.
2. `GET /api` — an `APIVersions` body (`{"kind": "APIVersions", "versions": ["v1"], ...}`).
3. `GET /api/v1` — an `APIResourceList` body mapping each `kind` (Node,
   Namespace, Pod, ConfigMap) to its resource name/namespaced-ness/verbs, so
   the DynamicClient can resolve `kind: Node` to the `/api/v1/nodes` path.
4. `GET /apis` — an (empty, in this stub's case) `APIGroupList` — probed
   unconditionally during discovery even though nothing here uses a named
   API group.

`stub_k8s_api.py` implements all four; skip any one of them and the very
first `k8s_info`/`k8s` task in a playbook 404s during discovery, before it
ever reaches the endpoint you actually wanted to test. This was found by
running `_smoke_test.yml` for real and iterating on the 404s one at a time —
see the file's own comments at the discovery-route registration for the
citation trail.

## Seeding STATE before a scenario

Both stub servers expose a module-level `STATE` dict and a `reset_state()`
function (see each file's own docstring for the exact shape). There are two
ways to seed fixtures, both implemented here:

1. **In-process**, when calling `start_server()` directly from a Python test
   script in the SAME process (e.g. the ad-hoc verification snippets used
   while building this harness) — mutate `STATE` (or call `reset_state()`
   first) before `start_server()`:

   ```python
   from stub_netbox_api import reset_state, STATE, start_server, stop_server

   reset_state()
   STATE["services"][42] = {
       "id": 42, "name": "nmos-crosspoint",
       "tags": [{"name": "app:nmos-crosspoint"}],
       "custom_fields": {},
   }
   server, thread, base_url = start_server()
   ```

2. **JSON fixture file**, for the normal ansible-playbook harness case where
   the actual server process is `start_stubs.py`'s **detached child** — a
   separate OS process, so nothing an ansible-playbook task does in-process
   can reach its `STATE` directly. `start_stubs.py` accepts
   `--k8s-seed-file <path>` / `--netbox-seed-file <path>`, each a JSON file
   merged into the respective `STATE` before the servers start listening:

   ```json
   // netbox seed
   {"services": {"42": {"id": 42, "name": "nmos-crosspoint", "tags": [...], "custom_fields": {}}}}
   ```

   ```json
   // k8s seed (nodes/namespaces/pods only — see stub_k8s_api's
   // _apply_k8s_seed docstring in start_stubs.py for why ConfigMaps aren't
   // seedable this way)
   {"nodes": [], "namespaces": ["nmos", "mxl"], "pods_forbidden": true}
   ```

   `_smoke_test.yml` uses this pattern (writes a fixture with
   `ansible.builtin.copy`, then passes `--netbox-seed-file` to
   `start_stubs.py`) — see it for a complete worked example.

   ConfigMaps specifically are best seeded **over HTTP** after the server is
   up (a real `POST .../configmaps` from a `pre_tasks` step) rather than via
   a seed file, since their storage key is a `(namespace, name)` tuple that
   doesn't round-trip through JSON.

## Calling `stub_helm.sh` as `helm`

`ansible.builtin.command: argv: [helm, ...]` (and `kubernetes.core.helm`,
which itself just shells out to the `helm` binary) resolve `helm` by exact
filename via `PATH`. `stub_helm.sh` is deliberately named for what it **is**
(a stub of helm), not what it must be **called**, so point `PATH` at a
**shim directory** containing a `helm` symlink to `stub_helm.sh`, not at
`tests/harness/` directly:

```yaml
- name: Create the PATH shim dir
  ansible.builtin.file:
    path: /tmp/l3-harness-bin
    state: directory

- name: Symlink helm -> stub_helm.sh
  ansible.builtin.file:
    path: /tmp/l3-harness-bin/helm
    src: "{{ playbook_dir }}/stub_helm.sh"
    state: link
    force: true

- name: Exercise a helm-calling task
  ansible.builtin.command:
    argv: [helm, list, -n, mxl, -o, json]
  environment:
    PATH: "/tmp/l3-harness-bin:{{ lookup('env', 'PATH') }}"
    L3_STUB_HELM_LIST_MXL: '[{"name":"mxl-fabrics-demo","revision":1,...}]'
```

Note `lookup('env', 'PATH')`, not `ansible_env.PATH` — the latter requires
`gather_facts: true` to be populated; a minimal `hosts: localhost` test
playbook that skips fact-gathering (as `_smoke_test.yml` does, for speed)
will silently end up with an unset `PATH` override otherwise (the
`environment:` key still gets set, just to an empty/undefined value, and the
task then falls through to whatever `helm` a real PATH resolves — this bit
the first version of `_smoke_test.yml` during development: it ran silently
against the *real* system `helm`, returning a real-but-irrelevant `[]`
instead of failing loudly).

Fixture env vars (`L3_STUB_HELM_LIST_<NS>`, `L3_STUB_HELM_VALUES_<RELEASE>`,
`L3_STUB_HELM_ROLLBACK_FAIL`, `L3_STUB_HELM_UNINSTALL_FAIL`) are documented
in `stub_helm.sh`'s own header comment, with the exact `<NS>`/`<RELEASE>`
uppercasing rule.

## Verification

`_smoke_test.yml` is this harness's own self-check (NOT a test of
`roles/l3_run_guard` itself — that role is out of scope for this harness's
own author, being edited concurrently elsewhere). Run it directly:

```bash
ansible-playbook tests/harness/_smoke_test.yml
```

It starts both stubs, then drives each harness surface with the same module
types the role's real tasks use: `kubernetes.core.k8s_info` (Node list),
`ansible.builtin.uri` (ConfigMap create/get/delete-with-precondition,
NetBox service get), and `ansible.builtin.command` (`helm list` via
`stub_helm.sh`) — asserting on each result.

## Known gaps / TODOs

- **`helm pull` / `helm template` model the chart PATH contract, not chart
  CONTENT.** `pull` creates `<untardir>/<chart-name>/Chart.yaml` (chart name
  from the last path segment of the chart ref) exactly like real
  `helm pull --untar --untardir`, and `template` **fails closed** with real
  helm's own `Error: path "<path>" not found` when handed a path that isn't
  on disk. What it renders is still an empty YAML document — so any scenario
  reaching a capacity render still needs `l3_override=true` for the
  resulting (genuine) missing-budget refusal, and a test that needs real
  demand numbers out of `l3_render_demand` still needs a real fixture chart
  here.

  These two cases are **paired and must stay paired** — relaxing `template`
  back to path-blind, or dropping `pull`'s chart subdirectory, re-opens the
  gap that hid umbrella #296: `capacity.yml` deleted the pulled chart dir
  before its topology group-render loop ran, and the old path-blind
  `template` happily "rendered" a directory that no longer existed, so CI
  stayed green while **every** live topology deploy failed in the gate.
  `tests/l3-topology-execution.yml` scenario 10 is the regression test and
  only discriminates because of this hardening.
- **`stub_k8s_api.py`'s PATCH support is a bonus**, not part of the original
  spec for this harness — added because `_supersede_sweep.yml` (and other
  `kubernetes.core.k8s: state: present` call sites) issue merge-style
  patches against existing ConfigMaps. It's a shallow `data`/`metadata.labels`
  merge only, not a full JSON Strategic Merge Patch implementation — good
  enough for this role's own usage (plain scalar `data` keys), not a general
  K8s patch semantics stand-in.
- **`kubernetes.core.helm` module's exact `uninstall` argv** wasn't verified
  against a real invocation (only `ansible.builtin.command`-driven `helm`
  calls were smoke-tested here) — `stub_helm.sh`'s `uninstall` case is
  tolerant of extra flags in any order, which should cover it, but hasn't
  been proven against the actual module-generated command line.
