#!/usr/bin/env python3
"""Stdlib-only stub Kubernetes API server for the l3_run_guard test harness.

Implements just the REST surface roles/l3_run_guard's tasks actually call:
  * ConfigMap create / get / list / update(PUT) / delete (with optional
    DeleteOptions.preconditions.uid) under /api/v1/namespaces/{ns}/configmaps
  * Node list under /api/v1/nodes (single fixed node by default, since the
    role hard-requires a single-node facility)
  * Namespace get-one under /api/v1/namespaces/{name}
  * Pod list under /api/v1/pods (cluster-wide, empty by default; a
    "forbidden" mode simulates a missing RBAC ClusterRole)
  * Deployment create / get (umbrella #306 design round #2) under
    /apis/apps/v1/namespaces/{ns}/deployments — added for
    switch_poll_upgrade_ready.yml / switch_poll_rollback_verify.yml's own
    kubernetes.core.k8s_info rollout-status polling (helm --wait is gone;
    see those files' own headers). No LIST — every real caller polls a
    single named Deployment.

No third-party dependencies (http.server / json / uuid / threading only) so
this runs inside a bare `python3` in an Ansible execution environment with
no pip installs.

Usage from a test script::

    from stub_k8s_api import reset_state, STATE, start_server, stop_server

    reset_state()
    STATE["nodes"] = []  # simulate a zero-node facility, e.g.
    server, thread, base_url = start_server()
    ...
    stop_server(server)

See tests/harness/README.md for the full pattern, including how a real
ansible-playbook run points kubernetes.core.k8s_info / ansible.builtin.uri
at this stub.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------


def _default_node(name: str = "test-node-1") -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Node",
        "metadata": {
            "name": name,
            "uid": str(uuid.uuid4()),
            "resourceVersion": "1",
            "labels": {},
            "annotations": {},
        },
        "spec": {},
        "status": {
            "allocatable": {"cpu": "4000m", "memory": "8Gi", "pods": "110"},
            "capacity": {"cpu": "4000m", "memory": "8Gi", "pods": "110"},
            "conditions": [
                {
                    "type": "Ready",
                    "status": "True",
                    "reason": "KubeletReady",
                    "message": "kubelet is posting ready status",
                }
            ],
            "nodeInfo": {
                "kubeletVersion": "v1.29.0",
                "architecture": "arm64",
                "operatingSystem": "linux",
                "kernelVersion": "6.1.0-stub",
                "osImage": "stub-os",
                "containerRuntimeVersion": "containerd://1.7.0",
            },
        },
    }


def _default_state() -> dict:
    return {
        # (namespace, name) -> stored ConfigMap object (dict)
        "configmaps": {},
        # (namespace, name) -> stored Deployment object (dict) — umbrella #306
        "deployments": {},
        # shared incrementing resourceVersion counter
        "resource_version": 0,
        # list of Node objects (dicts) — mutate before start_server() to
        # simulate zero-node / multi-node facilities
        "nodes": [_default_node()],
        # set of Namespace names that GET /api/v1/namespaces/{name} accepts
        "namespaces": {"nmos", "mxl"},
        # list of Pod objects (dicts) returned by GET /api/v1/pods
        "pods": [],
        # when True, GET /api/v1/pods returns 403 Forbidden (RBAC-probe path)
        "pods_forbidden": False,
        # TEST-ONLY (umbrella #202 WP3 R4a/P2-5): (namespace, name) -> int.
        # Simulates the create-then-read race l3_lock_acquire_one_attempt.yml
        # / _snapshot_create_one_attempt.yml retry over: while the counter
        # for a key is > 0, POST returns 409 WITHOUT actually storing the
        # object (so an immediate GET on that key finds nothing — the exact
        # "409 held, empty read" race), decrementing the counter each time.
        # Once it reaches 0, POST behaves normally again (stores + 201).
        # Never touched by any real role task — only a control-flow test's
        # own seeding.
        "phantom_conflicts": {},
        # TEST-ONLY (umbrella #306 design round #2): (kind, namespace, name)
        # -> list[dict]. Simulates state that changes OUT OF BAND while an
        # ansible task polls (a Deployment's rollout converging over
        # several GETs; a coordinator ConfigMap a restored pod republishes
        # target-info/epoch into) — generalizes phantom_conflicts's own
        # "consumed on access, TEST-ONLY" idiom from a bare counter to a
        # queue of deep-merge patches. One entry is popped and merged into
        # the stored object on each GET of that (kind, ns, name); once a
        # key's queue is empty, GETs simply keep returning whatever the
        # last merge left behind (steady state). Never touched by any real
        # role task — only a scenario's own seeding. See
        # _apply_get_patch_queue / _deep_merge below.
        "get_patch_queues": {},
    }


STATE: dict = _default_state()
_STATE_LOCK = threading.Lock()


def reset_state() -> None:
    """Reset STATE to fresh defaults. Call between test scenarios."""
    with _STATE_LOCK:
        STATE.clear()
        STATE.update(_default_state())


def _next_resource_version() -> str:
    STATE["resource_version"] += 1
    return str(STATE["resource_version"])


def _deep_merge(base: dict, patch: dict) -> dict:
    """Recursive merge-patch: a `None` value deletes the key, a dict value
    recurses, anything else overwrites — the same null-deletes-key idiom
    _patch_configmap already applies to its (flat) `data` map, generalized
    to arbitrary nesting for status/spec fields (umbrella #306 design
    round #2's own GET-time patch queue, see STATE["get_patch_queues"]).
    """
    merged = json.loads(json.dumps(base))
    for k, v in (patch or {}).items():
        if v is None:
            merged.pop(k, None)
        elif isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def _get_data_path(obj: dict, dotted_path: str):
    cur = obj
    for part in dotted_path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _apply_get_patch_queue(kind: str, ns: str, name: str, store: dict, key: tuple) -> dict:
    """TEST-ONLY (umbrella #306 design round #2): apply the FRONT queued
    GET-time patch for (kind, ns, name), if any is queued and its
    condition (if any) currently holds, deep-merging it into the STORED
    object (mutates `store[key]` in place, so the merge persists across
    subsequent GETs — an exhausted queue simply means later GETs keep
    returning whatever the last merge left behind, no separate "steady
    state" bookkeeping needed). Returns the (possibly just-merged) current
    object. A no-op when no queue is seeded for this key, once the queue
    is empty, or while the front entry's condition does not yet hold.

    Each queue entry is EITHER a bare patch dict (applied unconditionally
    on the next GET — fine for a resource with exactly one reader, e.g.
    the Deployment polls) OR {"when_absent": "<dotted.path>", "patch":
    {...}} — applied (and popped) only once that dotted path is currently
    absent/empty on the CURRENT stored object, otherwise left in place for
    a later GET. REQUIRED for a resource multiple DIFFERENT tasks read at
    different points in a real play (the coordinator ConfigMap: read
    pre-quiesce by switch_read_coordinator.yml, written by PHASE 1, then
    polled by PHASE 2 verify) — a bare FIFO-always-consume queue would let
    an EARLIER, unrelated read (e.g. the pre-quiesce one, which still sees
    the PRE-switch target-info/epoch) silently steal the entry meant to
    simulate the POST-switch republish, long before PHASE 1 has even
    cleared anything.
    """
    queue_key = (kind, ns, name)
    with _STATE_LOCK:
        queue = STATE["get_patch_queues"].get(queue_key)
        if queue:
            entry = queue[0]
            if isinstance(entry, dict) and "when_absent" in entry:
                current = _get_data_path(store[key], entry["when_absent"])
                if current is None or current == "":
                    queue.pop(0)
                    store[key] = _deep_merge(store[key], entry["patch"])
            else:
                queue.pop(0)
                store[key] = _deep_merge(store[key], entry)
        return store[key]


# ---------------------------------------------------------------------------
# K8s-shaped response helpers
# ---------------------------------------------------------------------------


def _status_body(code: int, reason: str, message: str, status: str = "Failure") -> dict:
    return {
        "kind": "Status",
        "apiVersion": "v1",
        "metadata": {},
        "status": status,
        "message": message,
        "reason": reason,
        "code": code,
    }


def _configmap_list_body(items: list) -> dict:
    return {
        "kind": "ConfigMapList",
        "apiVersion": "v1",
        "metadata": {"resourceVersion": str(STATE["resource_version"])},
        "items": items,
    }


def _node_list_body(items: list) -> dict:
    return {
        "kind": "NodeList",
        "apiVersion": "v1",
        "metadata": {"resourceVersion": str(STATE["resource_version"])},
        "items": items,
    }


def _pod_list_body(items: list) -> dict:
    return {
        "kind": "PodList",
        "apiVersion": "v1",
        "metadata": {"resourceVersion": str(STATE["resource_version"])},
        "items": items,
    }


def _namespace_body(name: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": name,
            "uid": str(uuid.uuid4()),
            "resourceVersion": "1",
        },
        "spec": {"finalizers": ["kubernetes"]},
        "status": {"phase": "Active"},
    }


def _default_deployment(ns: str, name: str) -> dict:
    """A steady-state, fully-ready, single-replica Deployment (umbrella
    #306 design round #2) — the shape switch_poll_upgrade_ready.yml /
    switch_poll_rollback_verify.yml actually read: rollout-status fields
    (observedGeneration/generation, updated/ready/available/unavailable
    Replicas) plus a pod template carrying a `status` container with an
    MXL_FLOW_ID env var (dmf-media's charts/mxl-fabrics-demo/templates/
    target.yaml — the real chart's own naming). A scenario POSTs this (or
    a variant) to seed a receiver's Deployment, then optionally queues
    GET-time patches (STATE["get_patch_queues"]) to simulate a rollout
    converging over several polls.
    """
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": ns,
            "uid": str(uuid.uuid4()),
            "resourceVersion": "1",
            "generation": 1,
            "labels": {},
            "annotations": {},
        },
        "spec": {
            "replicas": 1,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "status",
                            "env": [{"name": "MXL_FLOW_ID", "value": ""}],
                        }
                    ]
                }
            },
        },
        "status": {
            "observedGeneration": 1,
            "replicas": 1,
            "updatedReplicas": 1,
            "readyReplicas": 1,
            "availableReplicas": 1,
            "unavailableReplicas": 0,
            "conditions": [
                {"type": "Available", "status": "True", "reason": "MinimumReplicasAvailable"},
                {"type": "Progressing", "status": "True", "reason": "NewReplicaSetAvailable"},
            ],
        },
    }


# ---------------------------------------------------------------------------
# Label selector matching (only the subset this role's tasks send)
# ---------------------------------------------------------------------------


def _selector_clause_matches(labels: dict, clause: str) -> bool:
    clause = clause.strip()
    if not clause:
        return True
    if "!=" in clause:
        key, _, value = clause.partition("!=")
        return labels.get(key.strip()) != value.strip()
    if "=" in clause:
        key, _, value = clause.partition("=")
        return labels.get(key.strip()) == value.strip()
    # bare key => existence check, any value (including empty string)
    return clause in labels


def _label_selector_matches(labels: dict, selector: str) -> bool:
    if not selector:
        return True
    labels = labels or {}
    return all(_selector_clause_matches(labels, clause) for clause in selector.split(","))


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

_CM_COLLECTION_RE = re.compile(r"^/api/v1/namespaces/(?P<ns>[^/]+)/configmaps/?$")
_CM_ITEM_RE = re.compile(r"^/api/v1/namespaces/(?P<ns>[^/]+)/configmaps/(?P<name>[^/]+)/?$")
_NODES_RE = re.compile(r"^/api/v1/nodes/?$")
_PODS_RE = re.compile(r"^/api/v1/pods/?$")
_NAMESPACE_ITEM_RE = re.compile(r"^/api/v1/namespaces/(?P<name>[^/]+)/?$")
# umbrella #306 design round #2 — apps/v1 Deployment (get/create only, no list).
_DEPLOY_COLLECTION_RE = re.compile(
    r"^/apis/apps/v1/namespaces/(?P<ns>[^/]+)/deployments/?$"
)
_DEPLOY_ITEM_RE = re.compile(
    r"^/apis/apps/v1/namespaces/(?P<ns>[^/]+)/deployments/(?P<name>[^/]+)/?$"
)


class StubK8sHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "StubK8sAPI/1.0"

    def log_message(self, fmt, *args):  # noqa: A003 - silence default stderr spam
        pass

    # -- helpers -----------------------------------------------------------

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

    def _write_json(self, code: int, body: dict):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # -- verbs ---------------------------------------------------------

    def do_GET(self):  # noqa: N802 - http.server naming convention
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # -- API discovery, required by the python `kubernetes` client's
        # DynamicClient (used under the hood by kubernetes.core.k8s_info /
        # k8s) BEFORE it issues any real request: it GETs /version, then
        # /api and /api/v1 to map kind -> resource path. A bare JSON parse
        # (as ansible.builtin.uri does for the role's raw REST calls) never
        # hits these, so the role's own uri-based ConfigMap POST/PUT/DELETE
        # calls skip discovery entirely — only k8s_info/k8s (Node, Namespace,
        # Pod, and the ConfigMap list/patch paths) need it.
        if path == "/version":
            return self._get_version()
        if path == "/api":
            return self._get_api_versions()
        if path == "/api/v1":
            return self._get_api_v1_resources()
        if path == "/apis":
            return self._get_api_groups()
        if path == "/apis/apps/v1":
            return self._get_apps_v1_resources()

        m = _CM_ITEM_RE.match(path)
        if m:
            return self._get_configmap(m.group("ns"), m.group("name"))

        m = _CM_COLLECTION_RE.match(path)
        if m:
            return self._list_configmaps(m.group("ns"), query)

        m = _DEPLOY_ITEM_RE.match(path)
        if m:
            return self._get_deployment(m.group("ns"), m.group("name"))

        if _NODES_RE.match(path):
            return self._list_nodes()

        if _PODS_RE.match(path):
            return self._list_pods()

        m = _NAMESPACE_ITEM_RE.match(path)
        if m:
            return self._get_namespace(m.group("name"))

        self._write_json(404, _status_body(404, "NotFound", f"no route for GET {path}"))

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        m = _CM_COLLECTION_RE.match(parsed.path)
        if m:
            return self._create_configmap(m.group("ns"))
        m = _DEPLOY_COLLECTION_RE.match(parsed.path)
        if m:
            return self._create_deployment(m.group("ns"))
        if parsed.path == "/test-only/get-patch-queue":
            return self._queue_get_patch()
        self._write_json(404, _status_body(404, "NotFound", f"no route for POST {parsed.path}"))

    def do_PUT(self):  # noqa: N802
        parsed = urlparse(self.path)
        m = _CM_ITEM_RE.match(parsed.path)
        if m:
            return self._update_configmap(m.group("ns"), m.group("name"))
        self._write_json(404, _status_body(404, "NotFound", f"no route for PUT {parsed.path}"))

    def do_PATCH(self):  # noqa: N802
        # Bonus support (not in the original spec) for kubernetes.core.k8s
        # (state: present) merge-patches, e.g. _supersede_sweep.yml.
        parsed = urlparse(self.path)
        m = _CM_ITEM_RE.match(parsed.path)
        if m:
            return self._patch_configmap(m.group("ns"), m.group("name"))
        self._write_json(404, _status_body(404, "NotFound", f"no route for PATCH {parsed.path}"))

    def do_DELETE(self):  # noqa: N802
        parsed = urlparse(self.path)
        m = _CM_ITEM_RE.match(parsed.path)
        if m:
            return self._delete_configmap(m.group("ns"), m.group("name"))
        self._write_json(404, _status_body(404, "NotFound", f"no route for DELETE {parsed.path}"))

    # -- API discovery handlers ------------------------------------------

    def _get_version(self):
        self._write_json(
            200,
            {
                "major": "1",
                "minor": "29",
                "gitVersion": "v1.29.0-stub",
                "gitCommit": "0000000000000000000000000000000000stub",
                "gitTreeState": "clean",
                "platform": "linux/arm64",
            },
        )

    def _get_api_versions(self):
        self._write_json(
            200,
            {
                "kind": "APIVersions",
                "versions": ["v1"],
                "serverAddressByClientCIDRs": [
                    {"clientCIDR": "0.0.0.0/0", "serverAddress": "127.0.0.1"}
                ],
            },
        )

    def _get_api_v1_resources(self):
        # Only the resource kinds this role's tasks actually touch — enough
        # for the DynamicClient to resolve kind -> resource path for each.
        self._write_json(
            200,
            {
                "kind": "APIResourceList",
                "groupVersion": "v1",
                "resources": [
                    {
                        "name": "nodes",
                        "singularName": "node",
                        "namespaced": False,
                        "kind": "Node",
                        "verbs": ["get", "list", "watch"],
                    },
                    {
                        "name": "namespaces",
                        "singularName": "namespace",
                        "namespaced": False,
                        "kind": "Namespace",
                        "verbs": ["get", "list", "watch"],
                    },
                    {
                        "name": "pods",
                        "singularName": "pod",
                        "namespaced": True,
                        "kind": "Pod",
                        "verbs": ["get", "list", "watch"],
                    },
                    {
                        "name": "configmaps",
                        "singularName": "configmap",
                        "namespaced": True,
                        "kind": "ConfigMap",
                        "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
                    },
                ],
            },
        )

    def _get_api_groups(self):
        # Core/v1 kinds (Node, Namespace, Pod, ConfigMap) need no named API
        # group. umbrella #306 design round #2 adds the FIRST named-group
        # resource this stub serves (apps/v1 Deployment) — the DynamicClient
        # resolves apiVersion: apps/v1 via THIS list before it will ever GET
        # /apis/apps/v1 itself.
        self._write_json(
            200,
            {
                "kind": "APIGroupList",
                "apiVersion": "v1",
                "groups": [
                    {
                        "name": "apps",
                        "versions": [{"groupVersion": "apps/v1", "version": "v1"}],
                        "preferredVersion": {"groupVersion": "apps/v1", "version": "v1"},
                    }
                ],
            },
        )

    def _get_apps_v1_resources(self):
        self._write_json(
            200,
            {
                "kind": "APIResourceList",
                "groupVersion": "apps/v1",
                "resources": [
                    {
                        "name": "deployments",
                        "singularName": "deployment",
                        "namespaced": True,
                        "kind": "Deployment",
                        "verbs": ["get", "create"],
                    },
                ],
            },
        )

    # -- ConfigMap handlers --------------------------------------------

    def _create_configmap(self, ns: str):
        body = self._read_json_body() or {}
        name = (body.get("metadata") or {}).get("name")
        if not name:
            return self._write_json(
                400, _status_body(400, "BadRequest", "metadata.name is required")
            )
        key = (ns, name)
        with _STATE_LOCK:
            phantom_remaining = STATE["phantom_conflicts"].get(key, 0)
            if phantom_remaining > 0:
                STATE["phantom_conflicts"][key] = phantom_remaining - 1
                return self._write_json(
                    409,
                    _status_body(
                        409,
                        "AlreadyExists",
                        f"configmaps \"{name}\" already exists (TEST-ONLY phantom conflict, {phantom_remaining} remaining)",
                    ),
                )
            if key in STATE["configmaps"]:
                return self._write_json(
                    409,
                    _status_body(
                        409,
                        "AlreadyExists",
                        f"configmaps \"{name}\" already exists",
                    ),
                )
            stored = json.loads(json.dumps(body))  # deep copy
            stored.setdefault("apiVersion", "v1")
            stored.setdefault("kind", "ConfigMap")
            stored.setdefault("metadata", {})
            stored["metadata"]["namespace"] = ns
            stored["metadata"]["uid"] = str(uuid.uuid4())
            stored["metadata"]["resourceVersion"] = _next_resource_version()
            STATE["configmaps"][key] = stored
        self._write_json(201, stored)

    def _get_configmap(self, ns: str, name: str):
        key = (ns, name)
        stored = STATE["configmaps"].get(key)
        if stored is None:
            return self._write_json(
                404, _status_body(404, "NotFound", f"configmaps \"{name}\" not found")
            )
        current = _apply_get_patch_queue("ConfigMap", ns, name, STATE["configmaps"], key)
        self._write_json(200, current)

    def _list_configmaps(self, ns: str, query: dict):
        selector = ""
        if "labelSelector" in query and query["labelSelector"]:
            selector = query["labelSelector"][0]
        items = [
            obj
            for (obj_ns, _name), obj in STATE["configmaps"].items()
            if obj_ns == ns
            and _label_selector_matches((obj.get("metadata") or {}).get("labels", {}), selector)
        ]
        self._write_json(200, _configmap_list_body(items))

    def _update_configmap(self, ns: str, name: str):
        body = self._read_json_body() or {}
        key = (ns, name)
        with _STATE_LOCK:
            stored = STATE["configmaps"].get(key)
            if stored is None:
                return self._write_json(
                    404, _status_body(404, "NotFound", f"configmaps \"{name}\" not found")
                )
            incoming_rv = (body.get("metadata") or {}).get("resourceVersion")
            current_rv = stored["metadata"]["resourceVersion"]
            if incoming_rv != current_rv:
                return self._write_json(
                    409,
                    _status_body(
                        409,
                        "Conflict",
                        f"Operation cannot be fulfilled on configmaps \"{name}\": "
                        f"the object has been modified; please apply your changes "
                        f"to the latest version and try again "
                        f"(resourceVersion mismatch: have {current_rv}, got {incoming_rv})",
                    ),
                )
            updated = json.loads(json.dumps(body))
            updated.setdefault("apiVersion", "v1")
            updated.setdefault("kind", "ConfigMap")
            updated.setdefault("metadata", {})
            updated["metadata"]["namespace"] = ns
            updated["metadata"]["uid"] = stored["metadata"]["uid"]
            updated["metadata"]["resourceVersion"] = _next_resource_version()
            STATE["configmaps"][key] = updated
        self._write_json(200, updated)

    def _patch_configmap(self, ns: str, name: str):
        # Minimal strategic/merge-patch approximation: shallow-merge `data`
        # and `metadata.labels`, good enough for _supersede_sweep.yml's
        # `state: present` status-field patch via kubernetes.core.k8s.
        #
        # umbrella #201 WP3 fix-round (finding 7's own coverage caught
        # this): a `null` value for a `data` key is the standard K8s
        # merge-patch idiom for DELETING that single key (honored by a
        # real API server for map-typed fields) — this stub used to just
        # `dict.update()` the raw patch body, which sets the key to a
        # literal `None` rather than removing it, silently diverging from
        # real cluster behavior for rollback_coordinator_surface.yml's own
        # "remove this key" write.
        body = self._read_json_body() or {}
        key = (ns, name)
        with _STATE_LOCK:
            stored = STATE["configmaps"].get(key)
            if stored is None:
                return self._write_json(
                    404, _status_body(404, "NotFound", f"configmaps \"{name}\" not found")
                )
            merged = json.loads(json.dumps(stored))
            if "data" in body:
                merged.setdefault("data", {})
                for data_key, data_value in (body["data"] or {}).items():
                    if data_value is None:
                        merged["data"].pop(data_key, None)
                    else:
                        merged["data"][data_key] = data_value
            if "metadata" in body and "labels" in (body["metadata"] or {}):
                merged.setdefault("metadata", {}).setdefault("labels", {}).update(
                    body["metadata"]["labels"] or {}
                )
            merged["metadata"]["resourceVersion"] = _next_resource_version()
            STATE["configmaps"][key] = merged
        self._write_json(200, merged)

    def _delete_configmap(self, ns: str, name: str):
        body = self._read_json_body()
        key = (ns, name)
        with _STATE_LOCK:
            stored = STATE["configmaps"].get(key)
            if stored is None:
                return self._write_json(
                    404, _status_body(404, "NotFound", f"configmaps \"{name}\" not found")
                )
            if body:
                preconditions = body.get("preconditions") or {}
                want_uid = preconditions.get("uid")
                if want_uid and want_uid != stored["metadata"]["uid"]:
                    return self._write_json(
                        409,
                        _status_body(
                            409,
                            "Conflict",
                            f"Precondition failed: UID in precondition: {want_uid}, "
                            f"UID in object meta: {stored['metadata']['uid']}",
                        ),
                    )
            del STATE["configmaps"][key]
        self._write_json(200, _status_body(200, "", "configmap deleted", status="Success"))

    # -- Deployment handlers (umbrella #306 design round #2) ------------

    def _create_deployment(self, ns: str):
        # Mirrors _create_configmap's own shape: a scenario POSTs a full
        # Deployment body (typically _default_deployment(), possibly
        # combine()'d with overrides) to seed "an already-running viewer's
        # Deployment" before the real playbook run starts polling it.
        body = self._read_json_body() or {}
        name = (body.get("metadata") or {}).get("name")
        if not name:
            return self._write_json(
                400, _status_body(400, "BadRequest", "metadata.name is required")
            )
        key = (ns, name)
        with _STATE_LOCK:
            if key in STATE["deployments"]:
                return self._write_json(
                    409,
                    _status_body(409, "AlreadyExists", f"deployments.apps \"{name}\" already exists"),
                )
            stored = json.loads(json.dumps(body))  # deep copy
            stored.setdefault("apiVersion", "apps/v1")
            stored.setdefault("kind", "Deployment")
            stored.setdefault("metadata", {})
            stored["metadata"]["namespace"] = ns
            stored["metadata"]["uid"] = str(uuid.uuid4())
            stored["metadata"]["resourceVersion"] = _next_resource_version()
            STATE["deployments"][key] = stored
        self._write_json(201, stored)

    def _get_deployment(self, ns: str, name: str):
        key = (ns, name)
        stored = STATE["deployments"].get(key)
        if stored is None:
            return self._write_json(
                404, _status_body(404, "NotFound", f"deployments.apps \"{name}\" not found")
            )
        current = _apply_get_patch_queue("Deployment", ns, name, STATE["deployments"], key)
        self._write_json(200, current)

    # -- TEST-ONLY seeding endpoint (umbrella #306 design round #2) -----

    def _queue_get_patch(self):
        # TEST-ONLY: {"kind": "ConfigMap"|"Deployment", "namespace": ns,
        # "name": name, "patches": [ {...deep-merge patch...}, ... ]} —
        # appends to STATE["get_patch_queues"][(kind, ns, name)], consumed
        # FIFO by _apply_get_patch_queue on each matching GET. Lets a
        # scenario simulate state that changes OUT OF BAND across several
        # polls (a Deployment's rollout converging; a coordinator ConfigMap
        # a restored pod republishes target-info/epoch into) via ordinary
        # HTTP seeding, the SAME pattern every other scenario fixture
        # already uses (a real POST/PUT after the server starts) — never
        # touched by any real role task.
        body = self._read_json_body() or {}
        kind = body.get("kind")
        ns = body.get("namespace")
        name = body.get("name")
        patches = body.get("patches") or []
        if not kind or not ns or not name:
            return self._write_json(
                400, _status_body(400, "BadRequest", "kind, namespace, and name are required")
            )
        queue_key = (kind, ns, name)
        with _STATE_LOCK:
            STATE["get_patch_queues"].setdefault(queue_key, []).extend(patches)
        self._write_json(200, {"queued": len(patches)})

    # -- Node / Namespace / Pod handlers --------------------------------

    def _list_nodes(self):
        self._write_json(200, _node_list_body(STATE["nodes"]))

    def _get_namespace(self, name: str):
        if name in STATE["namespaces"]:
            return self._write_json(200, _namespace_body(name))
        self._write_json(404, _status_body(404, "NotFound", f"namespaces \"{name}\" not found"))

    def _list_pods(self):
        if STATE["pods_forbidden"]:
            return self._write_json(
                403,
                _status_body(
                    403,
                    "Forbidden",
                    "pods is forbidden: User \"stub\" cannot list resource "
                    '"pods" in API group "" at the cluster scope',
                ),
            )
        self._write_json(200, _pod_list_body(STATE["pods"]))


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


def start_server(port: int = 0):
    """Start the stub server in a background daemon thread.

    Returns (server, thread, base_url). Use port=0 to let the OS assign a
    free port (avoids collisions across parallel test runs).
    """
    server = ThreadingHTTPServer(("127.0.0.1", port), StubK8sHandler)
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
    print(f"K8S_STUB_URL={base_url}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop_server(server)
