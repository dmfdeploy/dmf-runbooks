#!/usr/bin/env python3
"""Stdlib-only stub NetBox API server for the l3_run_guard test harness.

Implements just the `ipam.services` REST subset roles/l3_run_guard's tasks
call via raw ansible.builtin.uri:
  * GET  /api/ipam/services/?name=<exact>   (list, filtered by exact name)
  * GET  /api/ipam/services/?tag=<exact>    (list, filtered by tag name)
  * GET  /api/ipam/services/{id}/           (get one)
  * POST /api/ipam/services/                (create, 201 — added for
    tests/l3-playbook-execution.yml scenario (10), the first scenario
    that reaches a provision.yml stage's own "create the catalog service"
    step; no prior scenario ever exercised this call)
  * PATCH /api/ipam/services/{id}/          (partial update — merges
    `tags`/`custom_fields` keys from the request body into the stored
    object)
  * DELETE /api/ipam/services/{id}/         (delete)

Plus a minimal `extras.tags` / `dcim.devices` surface (added for
tests/l3-playbook-execution.yml scenario (10) — the mxl/nmos-cpp/
nmos-crosspoint role's own provision.yml stage needs these BEFORE it ever
touches ipam.services):
  * GET  /api/extras/tags/?limit=200        (list, ignores limit — just
    returns every stored tag; an optional ?name=<exact> filter is also
    honored, used by netbox_catalog_common's ensure_workload_tag.yml)
  * POST /api/extras/tags/                  (create one, 201, echoes the
    request body's name/slug/color/description back with a fresh id)
  * GET  /api/extras/tags/{id}/             (get one, 404 when absent —
    added for umbrella #347's finalise-purge launcher, which readback-
    confirms a Tag object's own DELETE the same way
    _rollback_netbox_delete_one.yml already does for ipam.services)
  * DELETE /api/extras/tags/{id}/           (delete, 204/404 idempotent —
    umbrella #347: a Tag-object DELETE is genuinely new in this repo)
  * GET  /api/dcim/devices/?name=<x>        (single hardcoded device,
    `{"id": 1, "name": <x>}`, returned for ANY name query — good enough
    for the provision stage's own "device exists" assert; not a real
    per-device store)

Plus a minimal `dcim.sites` surface (umbrella #201 WP3 — topology_validate.yml's
own live target_facility agreement check, the first caller of this route):
  * GET  /api/dcim/sites/                   (list ALL stored sites, no
    filtering — mirrors extras.tags' own "ignore query params, return
    everything" shape; a test scenario seeds 0/1/2 sites via POST before
    running the nested playbook to drive the ambiguous/matching/mismatched
    cases)
  * POST /api/dcim/sites/                   (create one, 201, echoes the
    request body's own `slug` back with a fresh id — same shape as
    _create_tag)

umbrella #202 WP3 R6b (codex round-6, umbrella #267): optional request
logging for the `ipam.services` collection surface only — set
`L3_STUB_NETBOX_LOG=<path>` (as an env var on the process that starts
this server, e.g. via start_stubs.py's own `environment:` block in a test
playbook — this stub is a DETACHED child process, so the env var must be
present when THAT process is spawned, not just the later nested-playbook
run that talks to it over HTTP) to append one line per GET/POST against
`/api/ipam/services/` (NOT extras.tags/dcim.devices — irrelevant to the
netbox-identity single-source property this exists to probe):
  `GET services name=<x>`   — a `?name=<x>` collection GET (the exact
                              identity a caller queried BY)
  `POST services name=<x>`  — a create, `<x>` from the request body's
                              own `name` field (the exact identity a
                              caller is CREATING/MUTATING)
Lets a test observe, in call order, which concrete NetBox record name
each caller (e.g. snapshot.yml's own pre-run lookup vs. a catalog role's
own provision.yml lookup-then-create) actually used — the NetBox-axis
analogue of stub_helm.sh's L3_STUB_HELM_LOG (scenario 10's own
mechanism, this is scenario 11's).

umbrella #347 (finalise-purge launcher): `L3_STUB_NETBOX_STICKY_SERVICE_IDS`
(comma-separated ipam.services ids, same detached-process env-var
mechanism as L3_STUB_NETBOX_LOG) makes DELETE against those ids report 204
WITHOUT actually removing the record — simulating a real "delete didn't
take" backend inconsistency so a test can drive a genuine `dirty`
per-record classification (see _delete_service's own comment).

No third-party dependencies. Same reset_state()/STATE/start_server()/
stop_server() shape as stub_k8s_api.py for consistency — see
tests/harness/README.md.
"""
from __future__ import annotations

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def _log_services_request(line: str) -> None:
    """R6b: append one line to $L3_STUB_NETBOX_LOG if set — see this
    module's own docstring for the exact format/scope (ipam.services
    GET/POST only)."""
    log_path = os.environ.get("L3_STUB_NETBOX_LOG")
    if not log_path:
        return
    with open(log_path, "a") as f:
        f.write(line + "\n")

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------


def _default_state() -> dict:
    return {
        # service id (int) -> NetBox service object (dict)
        "services": {},
        "_services_next_id": 1,
        # extras.tags — tag id (int) -> NetBox tag object (dict). Minimal
        # in-memory list (mirrors "services" shape) — only what
        # provision.yml's tag-ensure loop needs: list (+ optional
        # ?name= filter) and create.
        "tags": {},
        "_tags_next_id": 1,
        # dcim.sites — umbrella #201 WP3 topology_validate.yml's own live
        # target_facility agreement check. Empty by default (the
        # 0-sites/ambiguous case is the DEFAULT state a scenario need not
        # seed anything for); a scenario POSTs 1 or 2 sites to drive the
        # matching/mismatched/ambiguous cases.
        "sites": {},
        "_sites_next_id": 1,
    }


STATE: dict = _default_state()
_STATE_LOCK = threading.Lock()


def reset_state() -> None:
    """Reset STATE to fresh defaults. Call between test scenarios."""
    with _STATE_LOCK:
        STATE.clear()
        STATE.update(_default_state())


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

_COLLECTION_RE = re.compile(r"^/api/ipam/services/?$")
_ITEM_RE = re.compile(r"^/api/ipam/services/(?P<id>\d+)/?$")
_TAGS_COLLECTION_RE = re.compile(r"^/api/extras/tags/?$")
_TAGS_ITEM_RE = re.compile(r"^/api/extras/tags/(?P<id>\d+)/?$")
_DEVICES_COLLECTION_RE = re.compile(r"^/api/dcim/devices/?$")
_SITES_COLLECTION_RE = re.compile(r"^/api/dcim/sites/?$")


class StubNetBoxHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "StubNetBoxAPI/1.0"

    def log_message(self, fmt, *args):  # noqa: A003
        pass

    # -- helpers -------------------------------------------------------

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return None
        raw = self.rfile.read(length)
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _write_json(self, code: int, body):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _write_empty(self, code: int):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- verbs -----------------------------------------------------------

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        m = _ITEM_RE.match(parsed.path)
        if m:
            return self._get_service(int(m.group("id")))

        if _COLLECTION_RE.match(parsed.path):
            return self._list_services(query)

        m = _TAGS_ITEM_RE.match(parsed.path)
        if m:
            return self._get_tag(int(m.group("id")))

        if _TAGS_COLLECTION_RE.match(parsed.path):
            return self._list_tags(query)

        if _DEVICES_COLLECTION_RE.match(parsed.path):
            return self._list_devices(query)

        if _SITES_COLLECTION_RE.match(parsed.path):
            return self._list_sites(query)

        self._write_json(404, {"detail": f"no route for GET {parsed.path}"})

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        if _COLLECTION_RE.match(parsed.path):
            return self._create_service()
        if _TAGS_COLLECTION_RE.match(parsed.path):
            return self._create_tag()
        if _SITES_COLLECTION_RE.match(parsed.path):
            return self._create_site()
        self._write_json(404, {"detail": f"no route for POST {parsed.path}"})

    def do_PATCH(self):  # noqa: N802
        parsed = urlparse(self.path)
        m = _ITEM_RE.match(parsed.path)
        if m:
            return self._patch_service(int(m.group("id")))
        self._write_json(404, {"detail": f"no route for PATCH {parsed.path}"})

    def do_DELETE(self):  # noqa: N802
        parsed = urlparse(self.path)
        m = _ITEM_RE.match(parsed.path)
        if m:
            return self._delete_service(int(m.group("id")))
        m = _TAGS_ITEM_RE.match(parsed.path)
        if m:
            return self._delete_tag(int(m.group("id")))
        self._write_json(404, {"detail": f"no route for DELETE {parsed.path}"})

    # -- handlers --------------------------------------------------------

    def _list_services(self, query: dict):
        name_filter = query.get("name", [None])[0]
        tag_filter = query.get("tag", [None])[0]

        if name_filter is not None:
            _log_services_request(f"GET services name={name_filter}")

        results = list(STATE["services"].values())
        if name_filter is not None:
            results = [s for s in results if s.get("name") == name_filter]
        if tag_filter is not None:
            results = [
                s
                for s in results
                if tag_filter in [t.get("name") for t in (s.get("tags") or [])]
            ]
        self._write_json(200, {"count": len(results), "results": results})

    def _get_service(self, service_id: int):
        svc = STATE["services"].get(service_id)
        if svc is None:
            return self._write_json(404, {"detail": "Not found."})
        self._write_json(200, svc)

    def _patch_service(self, service_id: int):
        body = self._read_json_body() or {}
        with _STATE_LOCK:
            svc = STATE["services"].get(service_id)
            if svc is None:
                return self._write_json(404, {"detail": "Not found."})
            updated = json.loads(json.dumps(svc))
            if "tags" in body:
                updated["tags"] = body["tags"]
            if "custom_fields" in body:
                updated.setdefault("custom_fields", {}).update(body["custom_fields"] or {})
            for key, value in body.items():
                if key not in ("tags", "custom_fields"):
                    updated[key] = value
            STATE["services"][service_id] = updated
        self._write_json(200, updated)

    def _create_service(self):
        body = self._read_json_body() or {}
        if body.get("name") is not None:
            _log_services_request(f"POST services name={body['name']}")
        with _STATE_LOCK:
            service_id = STATE["_services_next_id"]
            STATE["_services_next_id"] += 1
            svc = dict(body)
            svc["id"] = service_id
            STATE["services"][service_id] = svc
        self._write_json(201, svc)

    def _delete_service(self, service_id: int):
        # umbrella #347 — finalise-purge's own "call-success never equals
        # clean" readback discipline needs a way to simulate a DELETE that
        # REPORTS 204 but doesn't actually take (a real backend
        # inconsistency class, not something this stub can otherwise
        # produce — every other path here genuinely deletes on 204). A
        # scenario opts a specific id in via
        # $L3_STUB_NETBOX_STICKY_SERVICE_IDS (comma-separated ids, an env
        # var on the DETACHED stub process, same mechanism as
        # L3_STUB_NETBOX_LOG) — the record survives in STATE, so the
        # caller's own readback GET still returns 200, producing a genuine
        # "dirty" classification via l3_netbox_delete_dirty.
        sticky = {
            int(x) for x in os.environ.get("L3_STUB_NETBOX_STICKY_SERVICE_IDS", "").split(",") if x.strip()
        }
        with _STATE_LOCK:
            if service_id not in STATE["services"]:
                return self._write_json(404, {"detail": "Not found."})
            if service_id not in sticky:
                del STATE["services"][service_id]
        self._write_empty(204)

    def _list_tags(self, query: dict):
        name_filter = query.get("name", [None])[0]
        results = list(STATE["tags"].values())
        if name_filter is not None:
            results = [t for t in results if t.get("name") == name_filter]
        self._write_json(200, {"count": len(results), "results": results})

    def _create_tag(self):
        body = self._read_json_body() or {}
        with _STATE_LOCK:
            tag_id = STATE["_tags_next_id"]
            STATE["_tags_next_id"] += 1
            tag = {
                "id": tag_id,
                "name": body.get("name"),
                "slug": body.get("slug", ""),
                "color": body.get("color", ""),
                "description": body.get("description", ""),
            }
            STATE["tags"][tag_id] = tag
        self._write_json(201, tag)

    def _get_tag(self, tag_id: int):
        # umbrella #347 — finalise-purge's own Tag-object DELETE readback
        # (api/extras/tags/{id}/, expects 404 once gone) needs a real
        # get-one-by-id route, mirroring _get_service's own shape.
        tag = STATE["tags"].get(tag_id)
        if tag is None:
            return self._write_json(404, {"detail": "Not found."})
        self._write_json(200, tag)

    def _delete_tag(self, tag_id: int):
        # umbrella #347 — a Tag-object DELETE is genuinely new in this
        # repo; idempotent (204/404), mirrors _delete_service's own shape.
        with _STATE_LOCK:
            if tag_id not in STATE["tags"]:
                return self._write_json(404, {"detail": "Not found."})
            del STATE["tags"][tag_id]
        self._write_empty(204)

    def _list_devices(self, query: dict):
        # dcim.Device lookup — real callers filter by exact ?name=; this
        # stub is a single hardcoded device returned for ANY name query,
        # good enough for the provision stage's own "device exists"
        # assert (see this module's own docstring) — not a real
        # per-device store.
        name = query.get("name", ["stub-device"])[0]
        self._write_json(200, {"count": 1, "results": [{"id": 1, "name": name}]})

    def _list_sites(self, query: dict):
        # dcim.Site — no filtering, mirrors extras.tags' own shape; a
        # scenario seeds STATE["sites"] via POST before running the nested
        # playbook to drive the 0/1/2-site cases topology_validate.yml's
        # own live agreement check must distinguish.
        self._write_json(200, {"count": len(STATE["sites"]), "results": list(STATE["sites"].values())})

    def _create_site(self):
        body = self._read_json_body() or {}
        with _STATE_LOCK:
            site_id = STATE["_sites_next_id"]
            STATE["_sites_next_id"] += 1
            site = {"id": site_id, "name": body.get("name", ""), "slug": body.get("slug", "")}
            STATE["sites"][site_id] = site
        self._write_json(201, site)


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


def start_server(port: int = 0):
    """Start the stub server in a background daemon thread.

    Returns (server, thread, base_url). Use port=0 to let the OS assign a
    free port (avoids collisions across parallel test runs).
    """
    server = ThreadingHTTPServer(("127.0.0.1", port), StubNetBoxHandler)
    bound_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{bound_port}"
    return server, thread, base_url


def stop_server(server) -> None:
    server.shutdown()
    server.server_close()


if __name__ == "__main__":
    import sys
    import time

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    server, thread, base_url = start_server(port=port)
    print(f"NETBOX_STUB_URL={base_url}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop_server(server)
