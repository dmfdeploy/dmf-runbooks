"""umbrella #347 (Arc 2b) — finalise-purge launcher's pure NetBox-record
grouping/validation logic. Kept as filter plugins (mirrors this role's own
merge_owned_tags.py and l3_run_guard's filter_plugins/l3_budget.py
convention) rather than hand-rolled Jinja, for the same reason those files
give: real list/dict membership logic is straightforward Python and
awkward, easy-to-get-subtly-wrong Jinja.
"""

from __future__ import annotations


def l3_tag_names(tags):
    """Return the list of tag NAME strings from a NetBox service/tag
    record's own ``tags`` list (each ``{"name": "..."}`` dict). Mirrors
    dmf-cms's own ``_tag_names`` helper (media_workloads.py) byte-for-byte,
    on purpose — this is the SAME grouping the console performs, and the
    two must never drift (see l3_workload_members's own docstring)."""
    if not tags:
        return []
    return [t.get("name", "") if isinstance(t, dict) else str(t) for t in tags]


def l3_workload_members(services, workload_slug):
    """Filter a raw NetBox ``ipam.services`` list down to the members
    carrying an exact ``workload:<workload_slug>`` tag NAME.

    Mirrors dmf-cms's own grouping (media_workloads.py's
    ``_workload_assignment``/``list_workloads_grouped``) exactly: fetch
    every dmf-catalog-tagged service, then group by tag NAME client-side —
    never NetBox's own ``?tag=`` slug filter, which cannot express a
    colon-bearing tag name. The two implementations computing the SAME
    membership independently is deliberate (WO's own "exactly as the
    console groups them" requirement) — a drift here would mean this
    launcher's own idea of "the workload's members" silently disagrees with
    what an operator sees grouped in the console before requesting the
    purge.
    """
    if not services:
        return []
    wanted = "workload:" + workload_slug
    return [svc for svc in services if wanted in l3_tag_names(svc.get("tags"))]


def l3_lifecycle_tags(tags):
    """Return the subset of tag names on a record matching the
    ``lifecycle:`` prefix (ADR-0046 decision 4's owned namespace) — used to
    check a purge candidate carries EXACTLY one, and that it is precisely
    ``lifecycle:bootstrapped``."""
    return [n for n in l3_tag_names(tags) if n.startswith("lifecycle:")]


def l3_purge_fail_closed_violations(found_members, expected_ids):
    """Compute the WO's fail-closed violation set (frozen behavior #4) from
    a fresh membership read.

    Parameters
    ----------
    found_members : list
        The workload's current members (l3_workload_members's own output) —
        raw NetBox service dicts, each with at least ``id``/``tags``.
    expected_ids : list[int]
        ``purge_expected_service_ids`` — the console's own preflight read,
        supplied as a launch-time extra_var.

    Returns
    -------
    dict
        ``{"unexpected": [...ids...], "bad_lifecycle": [...ids...]}`` — both
        empty iff every found member is in the expected set AND carries
        EXACTLY the tag ``lifecycle:bootstrapped``. Delete nothing (frozen
        behavior #4) whenever either list is non-empty — the caller asserts
        on this return value directly, never delete-then-check.
    """
    expected = set(expected_ids or [])
    unexpected = []
    bad_lifecycle = []
    for member in found_members or []:
        member_id = member.get("id")
        if member_id not in expected:
            unexpected.append(member_id)
        lifecycle_tags = l3_lifecycle_tags(member.get("tags"))
        if lifecycle_tags != ["lifecycle:bootstrapped"]:
            bad_lifecycle.append(member_id)
    return {"unexpected": unexpected, "bad_lifecycle": bad_lifecycle}


class FilterModule:
    """Ansible filter plugin entry point."""

    def filters(self):
        return {
            "l3_tag_names": l3_tag_names,
            "l3_workload_members": l3_workload_members,
            "l3_lifecycle_tags": l3_lifecycle_tags,
            "l3_purge_fail_closed_violations": l3_purge_fail_closed_violations,
        }
