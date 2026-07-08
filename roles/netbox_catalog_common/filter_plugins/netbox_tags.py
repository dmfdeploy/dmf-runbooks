"""ADR-0046 decision 4 — owned-set tag-preservation merge filter.

Provides ``merge_owned_tags``: given the current tags on a NetBox service
record and the set of tags this launcher stage wants to own, returns the
PATCH-safe union — owned tags replaced wholesale, everything else preserved
(including ``workload:*`` and any future non-owned namespace).

Note: "preserved" means tag **membership by name** — the output is a
de-duplicated ``[{"name": "..."}]`` list suitable for a PATCH body.  The
full NetBox tag response dicts (id, slug, color, etc.) from the service
record are NOT carried through; only the tag name is retained.
"""

from __future__ import annotations


def merge_owned_tags(current_tags, owned_namespace, desired_tags):
    """Merge *desired_tags* into *current_tags*, preserving non-owned tags.

    Parameters
    ----------
    current_tags : list
        Tags from the existing NetBox service (each ``{"name": "..."}`` dict).
        May be empty or ``None`` for a brand-new service.
    owned_namespace : list[str]
        Exact names or prefixes that define the launcher's owned tag space.
        A tag name is owned iff it equals an entry or starts with a
        prefix entry (entries ending with ``:`` act as prefixes).
    desired_tags : list
        The tag set this stage wants to own — each ``{"name": "..."}`` dict.

    Returns
    -------
    list[dict]
        De-duplicated ``[{"name": "..."}]`` list safe for a PATCH body.
    """
    if owned_namespace is None:
        owned_namespace = []
    if desired_tags is None:
        desired_tags = []
    if current_tags is None:
        current_tags = []

    # Build the prefix set (entries ending with ':') and exact-match set.
    prefixes = [p for p in owned_namespace if p.endswith(":")]
    exacts = {p for p in owned_namespace if not p.endswith(":")}

    def _is_owned(name: str) -> bool:
        if name in exacts:
            return True
        return any(name.startswith(p) for p in prefixes)

    # Normalise current tags → list of name strings.
    current_names = [
        t["name"] if isinstance(t, dict) else str(t) for t in current_tags
    ]

    # Normalise desired tags → list of (name, dict) pairs.
    desired_pairs = [
        (t["name"] if isinstance(t, dict) else str(t),
         t if isinstance(t, dict) else {"name": str(t)})
        for t in desired_tags
    ]

    # Preserve non-owned current tags.
    preserved = [
        {"name": n} for n in current_names if not _is_owned(n)
    ]

    # Merge: preserved + desired, de-duplicated by name (desired wins).
    seen: set[str] = set()
    result: list[dict] = []
    for tag in preserved + [d for _, d in desired_pairs]:
        name = tag["name"]
        if name not in seen:
            seen.add(name)
            result.append(tag)

    return result


class FilterModule:
    """Ansible filter plugin entry point."""

    def filters(self):
        return {
            "merge_owned_tags": merge_owned_tags,
        }
