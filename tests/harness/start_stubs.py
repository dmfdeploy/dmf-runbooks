#!/usr/bin/env python3
"""CLI launcher: start both l3_run_guard test-harness stub servers.

Starts the K8s stub (stub_k8s_api.py) and NetBox stub (stub_netbox_api.py),
prints their base URLs to stdout as shell-evaluable lines, and writes a JSON
pidfile (pid + both ports/URLs) so a matching stop_stubs.py invocation can
find and stop them.

Usage from a shell (typically an ansible-playbook pre_tasks step):

    eval "$(python3 tests/harness/start_stubs.py --pidfile /tmp/l3-harness.json)"
    # now $K8S_STUB_URL / $NETBOX_STUB_URL are set in the current shell
    ...
    python3 tests/harness/stop_stubs.py --pidfile /tmp/l3-harness.json

Implementation note: the servers run as background daemon threads, and this
process must keep running for them to keep serving. By default this script
re-execs itself as a detached child process (subprocess.Popen with
start_new_session=True) that owns the actual servers, waits for that child
to signal readiness via the pidfile, then prints the URLs and exits — this
is deliberately NOT done via os.fork() after the server threads already
exist, since fork() only carries the calling thread into the child (the
ThreadingHTTPServer's serve_forever thread would silently vanish). Pass
--foreground to run the servers directly in this process instead (blocks;
useful for manual debugging or under a supervisor that already backgrounds
this command, e.g. `ansible.builtin.command` with `async`/`poll: 0`).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _apply_k8s_seed(seed_path: str) -> None:
    """Merge a JSON fixture file into stub_k8s_api.STATE before serving.

    Shape (all keys optional)::

        {
          "nodes": [ {...Node object...}, ... ],
          "namespaces": ["nmos", "mxl", ...],
          "pods": [ {...Pod object...}, ... ],
          "pods_forbidden": false,
          "phantom_conflicts": {"nmos|dmf-facility-lock": 2, ...}
        }

    ConfigMaps are intentionally NOT seedable this way (their storage key is
    a (namespace, name) tuple, not JSON-representable) — seed those over
    HTTP via a real POST after the server starts instead. ``phantom_conflicts``
    keys are ``"<namespace>|<name>"`` strings (JSON object keys can't be
    tuples either) — see stub_k8s_api.STATE's own docstring for what this
    TEST-ONLY field simulates (the create-then-read race, umbrella #202
    WP3 R4a/P2-5).
    """
    import json

    import stub_k8s_api

    with open(seed_path) as f:
        seed = json.load(f)
    if "nodes" in seed:
        stub_k8s_api.STATE["nodes"] = seed["nodes"]
    if "namespaces" in seed:
        stub_k8s_api.STATE["namespaces"] = set(seed["namespaces"])
    if "pods" in seed:
        stub_k8s_api.STATE["pods"] = seed["pods"]
    if "pods_forbidden" in seed:
        stub_k8s_api.STATE["pods_forbidden"] = bool(seed["pods_forbidden"])
    if "phantom_conflicts" in seed:
        stub_k8s_api.STATE["phantom_conflicts"] = {
            tuple(k.split("|", 1)): v for k, v in seed["phantom_conflicts"].items()
        }


def _apply_netbox_seed(seed_path: str) -> None:
    """Merge a JSON fixture file into stub_netbox_api.STATE before serving.

    Shape::

        {"services": {"<id>": {...NetBox service object incl. "id"...}, ...}}

    JSON object keys are always strings, so ids are converted back to int.
    """
    import json

    import stub_netbox_api

    with open(seed_path) as f:
        seed = json.load(f)
    for str_id, service in seed.get("services", {}).items():
        stub_netbox_api.STATE["services"][int(str_id)] = service


def _run_foreground(
    pidfile: str,
    k8s_port: int,
    netbox_port: int,
    k8s_seed_file: str | None,
    netbox_seed_file: str | None,
) -> None:
    import stub_k8s_api
    import stub_netbox_api

    stub_k8s_api.reset_state()
    stub_netbox_api.reset_state()
    if k8s_seed_file:
        _apply_k8s_seed(k8s_seed_file)
    if netbox_seed_file:
        _apply_netbox_seed(netbox_seed_file)

    k8s_server, _k8s_thread, k8s_url = stub_k8s_api.start_server(port=k8s_port)
    netbox_server, _netbox_thread, netbox_url = stub_netbox_api.start_server(port=netbox_port)

    record = {
        "pid": os.getpid(),
        "k8s_port": k8s_server.server_address[1],
        "netbox_port": netbox_server.server_address[1],
        "k8s_url": k8s_url,
        "netbox_url": netbox_url,
    }
    with open(pidfile, "w") as f:
        json.dump(record, f)

    while True:
        time.sleep(3600)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pidfile",
        default="/tmp/l3-run-guard-harness.json",
        help="Where to write {pid, k8s_port, netbox_port, k8s_url, netbox_url} JSON.",
    )
    parser.add_argument(
        "--k8s-port", type=int, default=0, help="Fixed K8s stub port (default: OS-assigned)."
    )
    parser.add_argument(
        "--netbox-port",
        type=int,
        default=0,
        help="Fixed NetBox stub port (default: OS-assigned).",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Run the servers directly in this process and block (no detach).",
    )
    parser.add_argument(
        "--k8s-seed-file",
        default=None,
        help="JSON fixture file merged into stub_k8s_api.STATE before serving "
        "(nodes/namespaces/pods — see stub_k8s_api's _apply_k8s_seed docstring).",
    )
    parser.add_argument(
        "--netbox-seed-file",
        default=None,
        help="JSON fixture file merged into stub_netbox_api.STATE before serving "
        "({\"services\": {\"<id>\": {...}}}).",
    )
    args = parser.parse_args()

    if args.foreground:
        _run_foreground(
            args.pidfile,
            args.k8s_port,
            args.netbox_port,
            args.k8s_seed_file,
            args.netbox_seed_file,
        )
        return 0  # unreachable, serve_forever loop never returns

    # Best-effort cleanup of a stale pidfile from a previous crashed run so
    # readiness-polling below can't mistake it for THIS invocation's record.
    if os.path.exists(args.pidfile):
        os.remove(args.pidfile)

    script = os.path.abspath(__file__)
    child_argv = [
        sys.executable,
        script,
        "--foreground",
        "--pidfile",
        args.pidfile,
        "--k8s-port",
        str(args.k8s_port),
        "--netbox-port",
        str(args.netbox_port),
    ]
    if args.k8s_seed_file:
        child_argv += ["--k8s-seed-file", args.k8s_seed_file]
    if args.netbox_seed_file:
        child_argv += ["--netbox-seed-file", args.netbox_seed_file]
    subprocess.Popen(
        child_argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    for _ in range(200):  # up to ~10s
        if os.path.exists(args.pidfile):
            with open(args.pidfile) as f:
                record = json.load(f)
            print(f"K8S_STUB_URL={record['k8s_url']}")
            print(f"NETBOX_STUB_URL={record['netbox_url']}")
            print(f"L3_HARNESS_PIDFILE={args.pidfile}")
            return 0
        time.sleep(0.05)

    print("start_stubs.py: timed out waiting for the detached stub server to start", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
