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

Also since umbrella #272/#273: two further independent static checks,
_check_none_sentinel_antipattern() and _check_type_debug_antipattern(),
scan every task file under roles/l3_run_guard/tasks/ and playbooks/*.yml
(via the shared _iter_production_yaml_string_leaves() parse, so production
YAML is only parsed once) for two version-portability anti-patterns that
each let the L3 gate refuse healthy environments on the AWX EE's older
ansible-core while passing clean on this repo's own dev harness — a
None-sentinel Jinja template gated by `is [not] none`, and a `type_debug`
result compared against a quoted type-name literal. See each function's
own docstring for its full story.

Usage:  python3 tests/scripts/check_playbook_structure.py
Exit 0 = every playbook checked below has the expected outer-block shape,
and no task file re-introduces the umbrella #272 or #273 anti-pattern.
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

import re
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

# umbrella #272 (regression guard, added same round as the fix) — scan
# scope for the None-sentinel anti-pattern below: this role's own task
# files plus the real entry playbooks. filter_plugins/*.py is Python, not
# Jinja/YAML, and is deliberately NOT scanned (see
# _check_none_sentinel_antipattern's own docstring for why the class of
# bug this bans is a YAML/Jinja-templating-only concern).
_ROLE_TASKS_DIR = _REPO_ROOT / "roles" / "l3_run_guard" / "tasks"

_NONE_SENTINEL_RE = re.compile(r"else\s+None\b")
_IS_NONE_GATE_RE = re.compile(r"\bis\s+not\s+none\b|\bis\s+none\b")

# umbrella #273 — the second version-portability class: `type_debug`
# reports a str SUBCLASS's own name (e.g. AnsibleUnsafeText) on older
# ansible-core rather than 'str', so any comparison of type_debug's
# output against a quoted primitive type-name literal is rendering-
# unstable across ansible-core versions. See
# _check_type_debug_antipattern's own docstring for the full story.
_TYPE_NAME_LITERAL_RE = re.compile(
    r"""['"](str|int|float|bool|list|dict|NoneType|unicode|AnsibleUnsafeText|AnsibleUnicode)['"]"""
)


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


def _walk_string_leaves(node: object, key_path: list[str]) -> list[tuple[list[str], str]]:
    """Recursively yield (key_path, value) for every string leaf in a
    parsed YAML tree. PyYAML's parse tree never contains comment text, so
    this can never false-positive on a comment merely DISCUSSING the
    banned pattern (this file's own header comments, or preflight.yml/
    rollback_snapshot_read.yml's umbrella #272 explainer comments, do —
    on purpose)."""
    hits: list[tuple[list[str], str]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            hits.extend(_walk_string_leaves(v, key_path + [str(k)]))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            hits.extend(_walk_string_leaves(v, key_path + [str(i)]))
    elif isinstance(node, str):
        hits.append((key_path, node))
    return hits


def _iter_production_yaml_string_leaves() -> list[tuple[Path, list[str], str]]:
    """Parse every task file under _ROLE_TASKS_DIR + every playbook under
    _PLAYBOOKS_DIR and yield (relative_path, key_path, string_value) for
    every string leaf found — the shared scan both umbrella #272's and
    #273's static anti-pattern checks below are built on, so production
    YAML is only parsed once regardless of how many patterns are banned
    against it. A YAML parse error on any file is reported as a single
    synthetic "hit" (key_path == ['<parse-error>']) so callers surface it
    as a normal error rather than needing their own separate handling."""
    hits: list[tuple[Path, list[str], str]] = []
    files = sorted(_ROLE_TASKS_DIR.rglob("*.yml")) + sorted(_PLAYBOOKS_DIR.rglob("*.yml"))
    for path in files:
        rel = path.relative_to(_REPO_ROOT)
        try:
            docs = list(yaml.safe_load_all(path.read_text()))
        except yaml.YAMLError as exc:
            hits.append((rel, ["<parse-error>"], str(exc)))
            continue
        for doc in docs:
            for key_path, value in _walk_string_leaves(doc, []):
                hits.append((rel, key_path, value))
    return hits


def _check_none_sentinel_antipattern(scan: list[tuple[Path, list[str], str]]) -> list[str]:
    """umbrella #272 — regression guard for the version-portability bug
    that motivated this check: a Jinja template classifying "no
    structural failure" via a bare `{{ ... else None }}` chained
    conditional, gated by `when: X is not none`. On ansible-core 2.20
    (this repo's dev harness) that template preserves the real Python
    None object, so the gate correctly stays closed. On the AWX EE's
    OLDER ansible-core the exact same template renders to the STRING ""
    instead — a defined, non-None value — so `is not none` misfires and
    refuses a HEALTHY environment (verified live: EE job 122 stored
    `_l3_preflight_failure: ""`). The harness's own dev-core interpreter
    cannot reproduce the EE's rendering (that's the whole portability
    bug), so no execution-level test run on this harness can catch a
    regression of this class — only a STATIC scan of the task files
    themselves can. Hence: both halves of the old pattern are banned
    outright, anywhere in this role's task files or the real entry
    playbooks — not just the two instances umbrella #272 actually found
    and fixed (roles/l3_run_guard/tasks/preflight.yml,
    rollback_snapshot_read.yml):

      1. a literal `else None` inside a Jinja template value (`set_fact:`,
         `vars:`, or any other templated field).
      2. an `is none` / `is not none` comparison anywhere in one of these
         files (`when:` gates are the observed case, but the ban is not
         limited to `when:` — the same rendering hazard applies to any
         comparison against a template-produced value).

    The fix for both: an empty-string sentinel (`else ''`) plus a
    `| length > 0` (or `| default('') | length > 0`) gate — identical
    semantics on every ansible-core version, because it never depends on
    None surviving as a distinct type through templating.

    filter_plugins/*.py is untouched and NOT scanned here: `else None` in
    real Python (no templating involved) is fine — see
    roles/l3_run_guard/filter_plugins/l3_budget.py's own l3_snapshot_refusal
    docstring for why ITS return contract still had to change (its result
    flows back into a Jinja template one hop downstream, so the class of
    bug reappears there even though the function itself is pure Python).
    """
    errors: list[str] = []
    for rel, key_path, value in scan:
        if key_path == ["<parse-error>"]:
            errors.append(f"{rel}: YAML parse error while scanning for static anti-patterns: {value}")
            continue
        where = f"{rel} ({'.'.join(key_path)})" if key_path else str(rel)
        if _NONE_SENTINEL_RE.search(value):
            errors.append(
                f"{where}: banned `else None` Jinja sentinel "
                "(umbrella #272 version-portability class — see "
                "_check_none_sentinel_antipattern's docstring). Use "
                "an empty-string sentinel (`else ''`) instead."
            )
        if _IS_NONE_GATE_RE.search(value):
            errors.append(
                f"{where}: banned `is none`/`is not none` comparison "
                "(umbrella #272 version-portability class — a "
                "template-produced None can render as a defined "
                "empty STRING on some ansible-core versions, so "
                "this comparison silently never matches there). Use "
                "`(var | default('')) | length > 0` instead."
            )
    return errors


def _check_type_debug_antipattern(scan: list[tuple[Path, list[str], str]]) -> list[str]:
    """umbrella #273 — regression guard for the SECOND member of the same
    version-portability class umbrella #272 found: `lock.yml`/
    `snapshot.yml` used to gate a ConfigMap manifest's data values with
    `map('type_debug') | select('ne', 'str')`. On ansible-core 2.20 (this
    dev harness, Data Tagging) a templated string reports plain 'str' via
    type_debug, so the harness's own execution suites always passed. On
    the AWX EE's OLDER ansible-core, that same templated string is
    `AnsibleUnsafeText` — a str SUBCLASS — and type_debug reports the
    subclass's own name, not 'str', so the membership check failed for
    values that were genuinely, correctly, valid strings (verified live:
    EE job 135, every manifest value a plain string). Exactly the same
    shape as #272: correct on this harness's core, wrong on the EE's,
    because type_debug's exact-name-string output isn't stable across
    ansible-core versions for anything that isn't a bare, un-subclassed
    builtin type — so this harness's execution suites can't catch a
    regression here either, only a static scan can.

    Banned outright in this role's task files and the real entry
    playbooks: `type_debug` appearing together with a quoted primitive
    type-name literal (`'str'`, `"int"`, etc.) in the SAME templated
    value — the shape of a type-name equality/membership check. The fix:
    the Jinja `string` TEST (`is string` / `reject('string')`), which
    uses isinstance() and so accepts str and every subclass, on any core.

    tests/ is OUT of scope for this check by construction (the shared
    scan only walks _ROLE_TASKS_DIR + _PLAYBOOKS_DIR) — tests/
    l3-budget-logic.yml's own (p) gate deliberately asserts type_debug's
    ACTUAL behavior on this dev harness's own known core (proving the
    `| string` coercion filter itself genuinely turns an int into a str),
    which is a legitimate, correct use this ban must not touch — see that
    gate's own umbrella #273 comment for why it isn't a re-test of the
    (now type_debug-free) production gate.
    """
    errors: list[str] = []
    for rel, key_path, value in scan:
        if key_path == ["<parse-error>"]:
            continue  # already reported once by _check_none_sentinel_antipattern
        if "type_debug" not in value:
            continue
        if _TYPE_NAME_LITERAL_RE.search(value):
            where = f"{rel} ({'.'.join(key_path)})" if key_path else str(rel)
            errors.append(
                f"{where}: banned `type_debug` compared against a quoted "
                "type-name literal (umbrella #273 version-portability "
                "class — see _check_type_debug_antipattern's docstring). "
                "Use the Jinja `string` test (`reject('string')` / "
                "`is string`) instead."
            )
    return errors


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

    production_scan = _iter_production_yaml_string_leaves()
    all_errors.extend(_check_none_sentinel_antipattern(production_scan))
    all_errors.extend(_check_type_debug_antipattern(production_scan))

    if all_errors:
        print(
            f"FAIL: {len(all_errors)} structural issue(s) found across the "
            f"{total} real entry playbooks and this role's task files:",
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
        "before it. umbrella #272: no task file under roles/l3_run_guard/"
        "tasks/ or playbooks/ contains the banned None-sentinel "
        "rendering-portability anti-pattern (`else None` template + `is "
        "[not] none` gate) either. umbrella #273: none contain the banned "
        "`type_debug` vs. quoted-type-name comparison either (same "
        "portability class — see _check_type_debug_antipattern's "
        "docstring)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
