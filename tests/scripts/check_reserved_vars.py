#!/usr/bin/env python3
"""umbrella #202 WP3 R5a (codex round-4 P1-1) — self-maintaining reserved-var
meta-test. Stdlib-only, no third-party deps (mirrors tests/harness's own
convention).

WHAT THIS PROVES: codex round-4's exact finding was that
_assert_reserved_vars.yml's blocklist covered SELECTED set_fact names but
omitted every `register:` target (e.g. _l3_lock_create, _l3_checkpoint_read)
— and since Ansible extra_vars outrank BOTH set_fact and registered
variables identically, an operator/console extra_var of the same name as any
omitted register target could silently override a module call's own result
(codex's executed proof: `-e '{"_l3_lock_create":{"status":201,...}}'` forged
a fake lock acquisition). A hand-maintained blocklist is structurally
incomplete — it silently drifts every time a new register:/set_fact: is
added to a task file without a matching reservation. This script makes that
drift IMPOSSIBLE to land unnoticed: it re-derives the full register:/
set_fact: name set from the ACTUAL task files (not from memory) every time
it runs, and fails loudly if anything in that set is not reserved.

umbrella #201 WP3 fix round (codex D2): this script used to scan ONLY
roles/l3_run_guard/tasks/*.yml — that scoping gap was itself a finding.
_l3_effective_chart_set_values was originally a set_fact: TASK inside
playbooks/launch-mxl-fabrics-demo.yml (an install-only alias never covered
by the reserved-var blocklist at all, since nothing scanned playbooks/),
and codex proved forging it directly made the real `helm upgrade --install`
consume different --set values than capacity.yml's gate ever validated —
a capacity/install divergence, the exact class of bug this whole mechanism
exists to close. Every launch-*.yml/teardown-*.yml/rollback-run.yml
playbook's own register:/set_fact: targets are now in scope too.

Usage:  python3 tests/scripts/check_reserved_vars.py
Exit 0 = every register:/set_fact: target in roles/l3_run_guard/tasks/*.yml
AND playbooks/*.yml is present in _assert_reserved_vars.yml's blocklist (or
is one of the explicitly-documented, deliberately-excluded caller-input
names).
Exit 1 = something is missing — printed, with the exact names.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_TASKS_DIR = _REPO_ROOT / "roles" / "l3_run_guard" / "tasks"
_PLAYBOOKS_DIR = _REPO_ROOT / "playbooks"
_RESERVED_FILE = _TASKS_DIR / "_assert_reserved_vars.yml"

_REGISTER_RE = re.compile(r"^\s*register:\s*([A-Za-z_][A-Za-z0-9_]*)\s*$")
_SETFACT_HEADER_RE = re.compile(r"ansible\.builtin\.set_fact:\s*$")
_SETFACT_KEY_RE = re.compile(r"^\s+([A-Za-z_][A-Za-z0-9_]*):")
_RESERVED_ENTRY_RE = re.compile(r"^\s*-\s*([A-Za-z_][A-Za-z0-9_]*)\s+is not defined")

# Names that are LEGITIMATE caller-supplied inputs on at least one entry
# path (never role-internal register/set_fact targets on that path) — see
# _assert_reserved_vars.yml's own header comment for the full per-name
# rationale. Kept here, in ONE place, so this script's notion of "excluded"
# always matches what the blocklist's own comment claims.
_LEGITIMATE_CALLER_INPUTS = {
    "l3_catalog_key",
    "l3_release",
    "l3_request_id",
    "l3_run_id",
    "l3_override_reason",
    "l3_owner",
    "l3_rollback_reason",
    "l3_target_namespace",
    "l3_chart_ref",
    "l3_chart_name",
    "l3_chart_version",
    "l3_chart_set_values",
    "l3_chart_local",
    "l3_override",
}


def _find_register_and_setfact_names() -> tuple[set[str], set[str]]:
    register_names: set[str] = set()
    setfact_names: set[str] = set()
    paths = sorted(_TASKS_DIR.glob("*.yml")) + sorted(_PLAYBOOKS_DIR.glob("*.yml"))
    for path in paths:
        lines = path.read_text().splitlines(keepends=True)
        for i, line in enumerate(lines):
            m = _REGISTER_RE.match(line)
            if m:
                register_names.add(m.group(1))
            if _SETFACT_HEADER_RE.search(line):
                indent = len(line) - len(line.lstrip())
                j = i + 1
                while j < len(lines):
                    nl = lines[j]
                    if nl.strip() == "":
                        j += 1
                        continue
                    nindent = len(nl) - len(nl.lstrip())
                    if nindent <= indent:
                        break
                    km = _SETFACT_KEY_RE.match(nl)
                    if km and nindent == indent + 2:
                        setfact_names.add(km.group(1))
                    j += 1
    return register_names, setfact_names


def _find_reserved_names() -> set[str]:
    reserved: set[str] = set()
    for line in _RESERVED_FILE.read_text().splitlines():
        m = _RESERVED_ENTRY_RE.match(line)
        if m:
            reserved.add(m.group(1))
    return reserved


def main() -> int:
    register_names, setfact_names = _find_register_and_setfact_names()
    all_internal = register_names | setfact_names
    reserved = _find_reserved_names()

    missing = (all_internal - _LEGITIMATE_CALLER_INPUTS) - reserved
    if missing:
        print(
            f"FAIL: {len(missing)} register:/set_fact: name(s) found in "
            f"roles/l3_run_guard/tasks/*.yml or playbooks/*.yml are NOT in "
            f"_assert_reserved_vars.yml's blocklist:",
            file=sys.stderr,
        )
        for name in sorted(missing):
            print(f"  - {name}", file=sys.stderr)
        print(
            "\nAdd each to _assert_reserved_vars.yml's `that:` list "
            "(ansible.builtin.assert, ' is not defined'), or add it to "
            "_LEGITIMATE_CALLER_INPUTS in this script with a documented "
            "reason if it is genuinely a caller-supplied input.",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK: all {len(all_internal - _LEGITIMATE_CALLER_INPUTS)} "
        f"role-internal register:/set_fact: names "
        f"({len(register_names)} register + {len(setfact_names)} set_fact, "
        f"{len(_LEGITIMATE_CALLER_INPUTS)} legitimate caller-input names "
        f"excluded) are present in _assert_reserved_vars.yml's blocklist."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
