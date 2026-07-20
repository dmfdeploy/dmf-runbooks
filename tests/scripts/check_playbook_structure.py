#!/usr/bin/env python3
"""umbrella #202 WP3 R5a-6 (codex round-4 P2-3) — static structural check that
every REAL entry playbook under playbooks/ actually wraps its whole
post-identity lifecycle in ONE outer block/rescue/always ("the Backbone",
landed R4a), with nothing able to sneak in front of it.

WHY THIS EXISTS: codex's round-4 review (P2-3) found that the existing
execution-test suite (tests/l3-control-flow.yml +
tests/harness/fixtures/fault_boundary.yml) only ever runs a FIXTURE that
REPLICATES the real playbooks' block/rescue/always shape with a trivial
stand-in mutation — it never runs, or even looks at, the actual 7 playbooks
under playbooks/. Because of that gap, two real bugs shipped undetected by
any test:

  1. launch-nmos-crosspoint.yml used to have its crosspoint-admin-password
     `assert` sitting BEFORE the outer block even started (fixed in this
     same round, R5a-3) — a missing secret produced a raw task-zero
     failure with no gate cleanup and no DMF_L3_OUTCOME marker at all. A
     fixture that already has the assert in the right place, by
     construction, can never catch a regression of THIS exact bug class —
     only looking at the real file's own task order can.
  2. several lock-lifecycle checkpoints were missing before terminal
     ConfigMap patches (fixed this round, R5a-4) — again invisible to a
     fixture that never had the defect shape to begin with.

This script re-derives the actual shape from the REAL playbook YAML (via a
proper parse tree — see the "why PyYAML, not regex" note below), so a
regression of either bug class — or a new one shaped like it (something
sneaking in front of the gate, or the gate's own include ending up outside
the block it's supposed to share fate with) — fails loudly here, fast,
with no cluster/stubs/EE needed. tests/l3-playbook-execution.yml (R5a-6's
other half) complements this with an actual stubbed EXECUTION proof of the
failure-path behavior these playbooks were shaped this way to guarantee.

WHY A REAL YAML PARSE TREE, NOT REGEX (unlike this directory's own
check_reserved_vars.py, which only needs to find lines matching a simple
register:/set_fact: pattern): this script needs to answer structural
questions — "is task N (a dict with a `block:` key) the FIRST element of
the play's `tasks:` list", "does that dict have BOTH `rescue:` and
`always:` sibling keys", "is a specific include_role found somewhere
INSIDE that dict's own `block:` list, not as a sibling before it" — that
is real list/dict tree-walking, not text-matching. PyYAML gives us the
actual tree; a regex sweep over indentation would be a hand-rolled, buggy
YAML parser in disguise.

Usage:  python3 tests/scripts/check_playbook_structure.py
Exit 0 = every playbook checked below has the expected outer-block shape.
Exit 1 = something is missing — printed, with the exact playbook + reason.

NOTE ON INTERPRETER: this script needs PyYAML, which is NOT guaranteed to
be importable under whatever bare `python3` resolves to on a given
operator's PATH (ansible-core itself depends on PyYAML, but that
dependency lives in ansible-core's OWN interpreter/venv, not necessarily
system python3 — this was found the hard way while building this script:
homebrew's python3 on the machine that authored this file has no PyYAML at
all, while ansible-core's own bundled interpreter does). Run this via its
ansible-playbook wrapper (tests/l3-playbook-structure.yml), which invokes
`{{ ansible_playbook_python }}` — ansible-core's own controller
interpreter — for exactly this reason. Running it directly with a bare
`python3` works too, IF that interpreter happens to have PyYAML; if not,
the ImportError below explains the fix rather than crashing raw.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print(
        "FAIL: PyYAML is not importable under this interpreter "
        f"({sys.executable}). This script parses real YAML list/dict "
        "structure (see its own module docstring for why regex won't do). "
        "Run it via its ansible-playbook wrapper instead — "
        "`ansible-playbook tests/l3-playbook-structure.yml` — which uses "
        "ansible_playbook_python (ansible-core's own controller "
        "interpreter, which always has PyYAML since ansible-core depends "
        "on it) rather than a bare, possibly-PyYAML-less `python3`. "
        "Alternatively, install PyYAML for this exact interpreter.",
        file=sys.stderr,
    )
    sys.exit(1)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PLAYBOOKS_DIR = _REPO_ROOT / "playbooks"

# The 7 real entry playbooks this round's Backbone (R4a) + R5a-3/R5a-4
# fixes apply to. "flow" selects which shape-expectations below apply —
# rollback-run.yml is deliberately SIMPLER than launch/teardown (see the
# "rollback" branch of _check_playbook below for exactly why).
_LAUNCH_PLAYBOOKS = [
    "launch-nmos-cpp.yml",
    "launch-mxl-fabrics-demo.yml",
    "launch-nmos-crosspoint.yml",
]
_TEARDOWN_PLAYBOOKS = [
    "teardown-nmos-cpp.yml",
    "teardown-mxl-fabrics-demo.yml",
    "teardown-nmos-crosspoint.yml",
]
_ROLLBACK_PLAYBOOK = "rollback-run.yml"

_GATE_ROLE = "l3_run_guard"


def _include_role_spec(task: dict) -> dict | None:
    """Return the include_role module args dict for a task, whichever of
    the FQCN or short module name it used, or None if this task isn't an
    include_role at all."""
    spec = task.get("ansible.builtin.include_role")
    if spec is None:
        spec = task.get("include_role")
    return spec if isinstance(spec, dict) else None


def _find_include_role(
    tasks: object, *, name: str | None = None, tasks_from: str | None = None
) -> list[dict]:
    """Recursively search a task list (descending into any nested
    block:/rescue:/always: on each task) for include_role tasks matching
    the given name/tasks_from (either filter optional — omit to match
    any). Returns the matching raw task dicts, not just their specs, so
    callers can report on them if needed."""
    found: list[dict] = []
    if not isinstance(tasks, list):
        return found
    for t in tasks:
        if not isinstance(t, dict):
            continue
        spec = _include_role_spec(t)
        if spec is not None:
            ok = True
            if name is not None and spec.get("name") != name:
                ok = False
            if tasks_from is not None and spec.get("tasks_from") != tasks_from:
                ok = False
            if ok:
                found.append(t)
        for key in ("block", "rescue", "always"):
            if key in t:
                found.extend(_find_include_role(t[key], name=name, tasks_from=tasks_from))
    return found


def _load_single_play(path: Path) -> tuple[dict | None, list[str]]:
    errors: list[str] = []
    docs = yaml.safe_load(path.read_text())
    if not isinstance(docs, list) or len(docs) != 1 or not isinstance(docs[0], dict):
        errors.append(
            f"{path.name}: expected the file's top level to be a one-item "
            f"list containing a single play (dict) — got "
            f"{type(docs).__name__}"
            + (f" of length {len(docs)}" if isinstance(docs, list) else "")
        )
        return None, errors
    return docs[0], errors


def _check_playbook(filename: str, flow: str) -> list[str]:
    """flow is one of 'launch', 'teardown', 'rollback'."""
    path = _PLAYBOOKS_DIR / filename
    if not path.is_file():
        return [f"{filename}: file not found under {_PLAYBOOKS_DIR}"]

    play, errors = _load_single_play(path)
    if play is None:
        return errors

    tasks = play.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        errors.append(f"{filename}: play has no non-empty 'tasks:' list")
        return errors

    # ── Requirement 1: the outer gate-through-terminal block is the FIRST
    #    top-level task — no task may precede it (this is exactly the
    #    class of bug that let the crosspoint credential-check bug through
    #    undetected: it used to sit before the block, R5a-3). We also
    #    check it's the ONLY top-level task, matching every real
    #    playbook's current shape — a future second top-level task
    #    (sibling to the block, e.g. in a stray pre_tasks/post_tasks) would
    #    sit outside the block's own rescue/always fate-sharing, the same
    #    structural risk as something sneaking in BEFORE it. ─────────────
    outer = tasks[0]
    if not isinstance(outer, dict) or "block" not in outer:
        errors.append(
            f"{filename}: the FIRST top-level task is not a `block:` — got "
            f"{sorted(outer.keys()) if isinstance(outer, dict) else outer!r}. "
            "No task may precede the L3 gate-through-terminal block (R5a-3: "
            "a credential assert used to sit here in "
            "launch-nmos-crosspoint.yml, producing a raw task-zero failure "
            "with no gate cleanup and no DMF_L3_OUTCOME marker at all — "
            "codex round-4 P1-3)."
        )
        return errors

    if len(tasks) != 1:
        errors.append(
            f"{filename}: the outer gate block is not the ONLY top-level "
            f"task — {len(tasks)} top-level tasks found under `tasks:`. "
            "Anything else at this level sits outside the block's own "
            "rescue:/always: fate-sharing."
        )

    inner_block = outer.get("block")
    if not isinstance(inner_block, list) or not inner_block:
        errors.append(f"{filename}: outer task's own `block:` is empty or not a list")
        inner_block = []

    has_rescue = "rescue" in outer
    has_always = "always" in outer

    # ── Requirement 2: rescue:/always: — expectations differ by flow. ────
    if flow in ("launch", "teardown"):
        if not has_rescue:
            errors.append(
                f"{filename}: outer block has no `rescue:` key — "
                "launch/teardown playbooks must share l3_run_guard's "
                "gate_rescue.yml here (R4a Backbone)."
            )
        elif not _find_include_role(outer.get("rescue"), tasks_from="gate_rescue"):
            errors.append(
                f"{filename}: outer block's `rescue:` does not include "
                "l3_run_guard's gate_rescue.yml (the shared launch/teardown "
                "cleanup handler)."
            )

        if not has_always:
            errors.append(
                f"{filename}: outer block has no `always:` key — every "
                "flow needs release.yml's idempotent lock-release backstop "
                "(R4a Backbone)."
            )
        elif not _find_include_role(outer.get("always"), name=_GATE_ROLE, tasks_from="release"):
            errors.append(
                f"{filename}: outer block's `always:` does not include "
                "l3_run_guard's release.yml backstop."
            )
    elif flow == "rollback":
        # rollback-run.yml is deliberately SIMPLER than launch/teardown —
        # its own header comment explains why: roles/l3_run_guard/tasks/
        # rollback.yml owns a BESPOKE internal rescue: of its own. THIS
        # calling playbook's outer block never needs (and does not have) a
        # rescue: of its own — only the idempotent release.yml backstop
        # via always:. This script therefore does NOT require a rescue:
        # key at this outer level for rollback-run.yml, unlike
        # launch/teardown.
        #
        # R5a-6 FINDING, FIXED same round (see tests/l3-playbook-execution.yml
        # scenario 7's own comment for the full writeup): this suite's own
        # construction surfaced that rollback.yml's stage 1 (identity) and
        # stage 2 (facility lock — where the shared post-lock
        # fault-injection boundary lives) used to run OUTSIDE
        # rollback.yml's own internal block/rescue (which used to wrap
        # only stages 3-6). A failure there propagated with ZERO
        # DMF_L3_OUTCOME marker, unlike every equivalent early failure on
        # launch/teardown. Fixed by moving stages 1-2 INSIDE rollback.yml's
        # own block, so its existing rescue now covers the whole flow —
        # this outer (rollback-run.yml) level still correctly needs no
        # rescue: of its own, since rollback.yml's internal one now
        # genuinely covers everything.
        if not has_always:
            errors.append(
                f"{filename}: outer block has no `always:` key — "
                "rollback's own idempotent release.yml backstop, added R5a."
            )
        elif not _find_include_role(outer.get("always"), name=_GATE_ROLE, tasks_from="release"):
            errors.append(
                f"{filename}: outer block's `always:` does not include "
                "l3_run_guard's release.yml backstop."
            )

        if not _find_include_role(inner_block, name=_GATE_ROLE, tasks_from="rollback"):
            errors.append(
                f"{filename}: outer block's own `block:` does not include "
                "l3_run_guard's rollback tasks_from — the authoritative "
                "rollback flow entry point."
            )
    else:
        errors.append(f"{filename}: internal test bug — unknown flow {flow!r}")

    # ── Requirement 3 (launch playbooks only, per this task's own scope):
    #    the l3_run_guard gate include must be INSIDE the outer block, not
    #    before it — and specifically the FIRST task inside it, matching
    #    every real launch playbook's current shape ("Include l3_run_guard
    #    role ... " is always the first block: item, immediately followed
    #    by the single-source identity check). ───────────────────────────
    if flow == "launch":
        gate_in_block = _find_include_role(inner_block, name=_GATE_ROLE)
        if not gate_in_block:
            errors.append(
                f"{filename}: no `include_role: name: {_GATE_ROLE}` (the "
                "gate) found INSIDE the outer block's own `block:` list."
            )
        else:
            first = inner_block[0] if inner_block else None
            first_spec = _include_role_spec(first) if isinstance(first, dict) else None
            if not (first_spec and first_spec.get("name") == _GATE_ROLE):
                errors.append(
                    f"{filename}: `include_role: name: {_GATE_ROLE}` (the "
                    "gate) is not the FIRST task inside the outer block."
                )

        # Belt-and-suspenders: a gate include hiding in pre_tasks: would
        # run OUTSIDE the outer block entirely (pre_tasks: always runs
        # before tasks:, block or not) — same structural risk as sitting
        # before it inside tasks:.
        if _find_include_role(play.get("pre_tasks"), name=_GATE_ROLE):
            errors.append(
                f"{filename}: `include_role: name: {_GATE_ROLE}` found in "
                "`pre_tasks:` — this runs BEFORE the outer block "
                "(block or not), the same structural risk as sitting "
                "before it inside `tasks:`."
            )

    return errors


def main() -> int:
    all_errors: list[str] = []
    for fn in _LAUNCH_PLAYBOOKS:
        all_errors.extend(_check_playbook(fn, "launch"))
    for fn in _TEARDOWN_PLAYBOOKS:
        all_errors.extend(_check_playbook(fn, "teardown"))
    all_errors.extend(_check_playbook(_ROLLBACK_PLAYBOOK, "rollback"))

    total = len(_LAUNCH_PLAYBOOKS) + len(_TEARDOWN_PLAYBOOKS) + 1

    if all_errors:
        print(
            f"FAIL: {len(all_errors)} structural issue(s) found across the "
            f"{total} real entry playbooks:",
            file=sys.stderr,
        )
        for e in all_errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(
        f"OK: all {total} real entry playbooks (playbooks/*.yml) wrap "
        "their entire post-identity lifecycle in ONE outer block as the "
        "sole/first top-level task, with the rescue:/always: keys their "
        "flow requires (gate_rescue.yml + release.yml for launch/teardown; "
        "release.yml only for rollback, by design — see this script's own "
        "rollback-branch comment), and (launch playbooks) the l3_run_guard "
        "gate include sits as the first task inside that block, not "
        "before it."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
