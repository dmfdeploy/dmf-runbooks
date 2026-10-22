"""dmfdeploy/dmfdeploy#474 — make one caller-supplied value safe to render
inside an outcome marker line (DMF_L3_OUTCOME, DMF_L3_SWITCH_OUTCOME).

The console parses a marker as "<PREFIX>: <token> key=value key=value ...",
one line, fields split on spaces. run_id, request_id and receiver_instance
arrive as caller extra_vars. A value carrying a newline used to split the
marker into two lines (the second one shaped like a marker of the
caller's choosing), and a value carrying a space used to add a key=value
field of the caller's choosing.

Pure function only (same posture as l3_budget.py/l3_topology.py).
"""

from __future__ import annotations

from urllib.parse import quote

# '<' and '>' cannot end a line or a field, and keeping them lets the
# '<unknown>' placeholder some call sites default to render unchanged.
_SAFE = "<>"


def l3_marker_value(value) -> str:
    """Percent-encode every character of ``value`` outside
    ``[A-Za-z0-9_.~<>-]``, so the result is one marker field value with no
    whitespace, no control character and no '='. Lossless (a reader can
    decode it), and a no-op for every well-formed value: a 32-char hex
    run_id/request_id and a Helm release name come back unchanged."""
    return quote(str(value), safe=_SAFE)


class FilterModule:
    """Ansible filter plugin entry point."""

    def filters(self):
        return {
            "l3_marker_value": l3_marker_value,
        }
