"""A fake Tiiny, good enough to develop and test against with no hardware.

Almost every response shape here comes from a recorded artefact rather than from
guesswork. The five that could not be are listed at the bottom of this docstring
and marked at each site:

  * /Users/sem/code/tiiny/spec-8800.json, the gateway's own OpenAPI document,
    for OpenAIModelList and OpenAIModel (required: name, fullname, size, params,
    type, id) and for NpuStatusResponse (hm_smi_available, devices, cpu, memory,
    occupants, npu, with temp_c and power_w per device).
  * /Users/sem/code/tiiny/RUNBOOK.md for the bodies the specs declare only as
    free-form objects: the NPU units budget, the running-models payload, the
    start and stop replies, and the not-loaded error.
  * /Users/sem/code/tiiny/CAPABILITIES.md for the behaviour: one inference at a
    time, no batching, and the NPU unit cost of each model.

The two failures that matter are reproduced on purpose, because code that has
never met them is not tested:

  150004  a second inference arriving mid-inference comes back as HTTP 200 with
          {"code": 150004, "message": "The operation failed to complete."}
  504     a request that runs past the gateway ceiling is closed. Scaled down
          here from the measured ~222s so a test can exercise it in under a
          second.

All three services share one port. The real device spreads them over 8800, 80
and 39218, but the paths do not collide, so one listener is enough and it keeps
the fake to a single address.

With vhost_only=True the fake reproduces the firmware where port 8800 is closed
to the LAN: a gateway path then needs the Host header the vendor CLI sends
(p8800.api.tiiny for the model and NPU endpoints, openai.api.tiiny for the
OpenAI-compatible surface), and without one it answers as the device management
plane, exactly as port 80 does on real hardware.

UNVERIFIED SHAPES
-----------------
Three responses still cannot be sourced from a recording. The specs declare them
as free-form objects and no live body has been captured, so what is below is
inferred from adjacent evidence and is marked UNVERIFIED at each site. Confirm
each against a real device before trusting it; the checklist in the README
covers them.

  1. POST /api/v1/models/{id}/download/stream, the SSE frame body. The spec
     documents that the endpoint streams progress and says nothing about the
     frame. Inferred from the get_progress fields plus speed_human, which is a
     real OpenAIModel field. Confirming it means starting a download, which is
     a write, so it has been left alone.
  2. GET /api/v1/models/{id}/get_progress. Free-form in the spec. The field
     names come from tiiny-hud, which reads progress and status off it and
     works, so this is second hand rather than guessed.
  3. devices[].temp_c and power_w in /api/v1/npu/status. Declared by the spec;
     confirmed present and null on live firmware, so the shape is right and
     there is still no temperature on this REST surface.

Verified against live hardware on 2026-09-13, and no longer guesses:

  * GET /device.json, including the serial_number field, the discovery token,
    the USB /30 and the per-interface address list.
  * The UDP responder on 39217: send the advertised token, get device.json back.
  * GET /api/v1/models/storage, and instance_id in the running instances list.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .device import GATEWAY_VHOST, OPENAI_VHOST

KEY = "00000000-0000-4000-8000-000000000000"

# The longest completion the fake will produce. A real 35B on this hardware runs
# at about 26 tok/s, so anything longer just makes a demo wait.
TOKEN_CAP = 400
# Measured prompt-processing rate from API.md, used for the timings block so the
# prefill benchmark draws a real curve.
PREFILL_TOK_S = 28.2
# A pace for demos: fast enough to watch, slow enough that streaming looks like
# streaming and the concurrency queue is visible. Tests leave it at 0.
DEMO_TOKEN_DELAY = 0.012
# time.sleep cannot pace individual tokens: asked for 12 ms it can take 100, so
# eighty per-token sleeps turned a one second answer into eight. Pacing runs off
# a deadline instead and only sleeps when at least this much is owed, which
# makes tokens arrive in small bursts and keeps the total honest.
MIN_SLEEP = 0.05
# Pause between download progress frames in demo mode, so the bar can be seen.
DEMO_DOWNLOAD_STEP = 0.4

# Unit costs are the measured ones from CAPABILITIES.md. The capabilities list
# is the device's own answer about what a model is for, read off live firmware
# on 2026-09-14: "main" is the chat runtime and every other value is a model
# that will never answer a chat completion.
CATALOG = [
    {"model_id": "deepreinforce-ai/Ornith-1.0-35B", "type": "Image-Text-to-Text",
     "params": "35B", "size": 18_000_000_000, "npu_usage": 50,
     "capabilities": ["main"]},
    {"model_id": "Qwen/Qwen3.6-35B-A3B", "type": "Image-Text-to-Text",
     "params": "35B-A3B", "size": 18_000_000_000, "npu_usage": 50,
     "capabilities": ["main"]},
    {"model_id": "Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo", "type": "Text Generation",
     "params": "30B-A3B", "size": 15_200_000_000, "npu_usage": 45,
     "capabilities": ["main"]},
    {"model_id": "Tongyi-MAI/Z-Image-Turbo", "type": "Text-to-Image",
     "params": "6B", "size": 10_200_000_000, "npu_usage": 32,
     "capabilities": ["image"]},
    {"model_id": "Qwen/Qwen3-ASR-1.7B", "type": "ASR",
     "params": "1.7B", "size": 3_600_000_000, "npu_usage": 7,
     "capabilities": ["audio"]},
    {"model_id": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", "type": "Text-to-Speech",
     "params": "1.7B", "size": 2_400_000_000, "npu_usage": 7,
     "capabilities": ["voice"]},
    {"model_id": "Qwen/Qwen3-TTS-12Hz-1.7B-Base", "type": "Text-to-Speech",
     "params": "1.7B", "size": 2_500_000_000, "npu_usage": 5,
     "capabilities": ["voice"]},
    {"model_id": "Qwen/Qwen3-Reranker-0.6B", "type": "Text Reranking",
     "params": "0.6B", "size": 700_000_000, "npu_usage": 2,
     "capabilities": ["rerank"]},
    {"model_id": "Qwen/Qwen3-Embedding-0.6B", "type": "Text Embedding",
     "params": "0.6B", "size": 900_000_000, "npu_usage": 1,
     "capabilities": ["embedding"]},
    {"model_id": "openai/gpt-oss-20b", "type": "Text Generation",
     "params": "20B", "size": 12_000_000_000, "npu_usage": 30,
     "capabilities": ["main"]},
    {"model_id": "zai-org/GLM-4.7-Flash", "type": "Text Generation",
     "params": "9B", "size": 6_000_000_000, "npu_usage": 12,
     "capabilities": ["main"]},
    # No capabilities at all, on purpose: older catalogue rows carry only a
    # type, and the classifier has to fall back to it rather than refuse.
    {"model_id": "PaddlePaddle/PP-OCRv6-Small", "type": "Image-to-Text",
     "params": "2.48M", "size": 114_000_000, "npu_usage": 0},
]

# A device that has a speech model loaded next to a chat model is the ordinary
# case, not an exotic one: the box this was checked against had an embedding,
# an image and a text-to-speech model running alongside one chat model.
DEFAULT_INSTALLED = ["deepreinforce-ai/Ornith-1.0-35B",
                     "Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo",
                     "Qwen/Qwen3-Embedding-0.6B",
                     "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
                     "Qwen/Qwen3-ASR-1.7B"]

# Filler for the fake's answers. Long and varied enough that a few hundred
# tokens of it reads like prose rather than one sentence on a loop, which
# matters because these answers end up in screenshots.
SAMPLE = (
    "Memory bandwidth sets the ceiling here. The accelerator reads the active "
    "weights once per token, so tokens per second is bandwidth divided by bytes "
    "read per token, and no amount of scheduling changes that arithmetic. "
    "Adding a second caller does not add throughput, because the two requests "
    "are not batched: they are queued, and the queue is in the runtime rather "
    "than in the API layer, so it cannot be tuned away from the outside. "
    "What does move the number is the shape of the model. A mixture of experts "
    "reads only its active parameters for each token, so a larger model with a "
    "small active set can decode faster than a smaller dense one. "
    "The practical consequences are worth stating plainly. Sequential pipelines "
    "cost nothing extra, since the stages were going to run one at a time "
    "anyway. Long single responses are the expensive case, and they are also "
    "the case a per-request time limit will cut off first. Batched work for "
    "several people at once is the case this silicon is worst at, and the one "
    "worth moving somewhere else. ")


class FakeState:
    """One fake device's mutable state, guarded for concurrent handlers."""

    def __init__(self, index=1, installed=None, loaded=None, npu_total=100,
                 token_delay=0.0, request_ceiling=None, serial=None,
                 vhost_only=False):
        # vhost_only reproduces the firmware where port 8800 is closed to the
        # LAN: the gateway is then only reachable on port 80 by virtual host,
        # and a request without the right Host header gets the management
        # surface instead.
        self.vhost_only = vhost_only
        self.vhost_hits = 0
        self.index = index
        self.serial = serial or "TNYF260900000000%02dQ" % index
        self.name = "tiiny-fake-%d" % index
        self.npu_total = npu_total
        self.token_delay = token_delay
        # Scaled stand-in for the measured ~222s gateway ceiling.
        self.request_ceiling = request_ceiling
        self.device_id = "8804fa89557e415f8055cf77d98c8ac%02d" % index
        # Addresses the fake claims in its device.json. Tests override them so a
        # box can be seen on two planes.
        self.lan_address = None
        self.usb_address = None
        self.installed = list(DEFAULT_INSTALLED if installed is None else installed)
        first = [m for m in self.installed if self._cost(m) >= 10][:1]
        self.loaded = list(first if loaded is None else loaded)
        self.downloads = {}
        self.guard = threading.Lock()
        self.inference = threading.Lock()
        self.collisions = 0
        self.served = 0

    # ---------------------------------------------------------------- helpers
    def _cost(self, model_id):
        for row in CATALOG:
            if row["model_id"] == model_id:
                return row["npu_usage"]
        return 1

    def _row(self, model_id):
        for row in CATALOG:
            if row["model_id"] == model_id:
                return row
        return {"model_id": model_id, "type": "Text Generation", "params": "?",
                "size": 0, "npu_usage": 1, "capabilities": ["main"]}

    def units_used(self):
        return sum(self._cost(m) for m in self.loaded)

    # --------------------------------------------------------------- payloads
    def models_payload(self):
        """OpenAIModelList. Fields track spec-8800.json's OpenAIModel."""
        data = []
        for model_id in self.installed:
            row = self._row(model_id)
            short = model_id.split("/")[-1]
            data.append({
                "name": short, "fullname": model_id, "size": row["size"],
                "toolkit_size": 0, "runtime_size": 0, "total_size": row["size"],
                "params": row["params"], "type": row["type"], "id": model_id,
                "capabilities": list(row.get("capabilities") or []),
                "model_id": model_id, "display_name": short,
                "hf_repo_id": model_id, "object": "model", "created": 0,
                "owned_by": "Model store", "status": "downloaded",
                "download_status": "downloaded", "progress": 100.0,
                "npu_usage": row["npu_usage"]})
        return {"object": "list", "data": data}

    def running_payload(self):
        instances = []
        for offset, model_id in enumerate(self.loaded):
            # All four verified against live firmware 2026-09-13. The real
            # payload carries more per instance (created_at, capabilities,
            # active_request_count); these are the ones Pocket reads.
            row = self._row(model_id)
            instances.append({"model_id": model_id, "port": 9098 + offset,
                              "npu_usage": self._cost(model_id),
                              "type": row["type"],
                              "capabilities": list(row.get("capabilities") or []),
                              "instance_id": "fake-%s-%d" % (self.serial, offset)})
        return {"running": list(self.loaded), "instances": {"running": instances}}

    def units_payload(self):
        used = self.units_used()
        return {"npu_total": self.npu_total, "npu_used": used,
                "npu_available": self.npu_total - used,
                "models": [{"model_id": m, "npu_usage": self._cost(m)}
                           for m in self.loaded]}

    def npu_status_payload(self):
        """NpuStatusResponse. temp_c and power_w are declared by the spec and
        have never been observed non-null on real firmware, so they stay null
        here too. Inventing a temperature would be the one lie that matters."""
        return {"hm_smi_available": True,
                "devices": [{"device_id": 0, "util_percent": 0.0,
                             "mem_used_mb": 1024 * 2 + self.units_used() * 420,
                             "mem_total_mb": 49024, "temp_c": None, "power_w": None,
                             "model": "Houmo dNPU", "sn": self.serial, "bdf": None}],
                "cpu": {"total_percent": 7.5 + self.index},
                "memory": {"usage_percent": 22.0},
                "occupants": [{"app_id": "ainode-pocket",
                               "activate_model": list(self.loaded)}],
                "npu": {"status": "ready", "status_message": "ok"}}

    def sys_status_payload(self):
        used_mb = 1024 * 2 + self.units_used() * 420
        return {"cpu": {"total_percent": 7.5 + self.index,
                        "per_core_percent": [4.0 + (i * 1.5) % 30 for i in range(12)]},
                "memory": {"usage_percent": 22.0, "used_bytes": 7_340_032_000,
                           "total_bytes": 33_390_000_000},
                "disk": {"used_bytes": 160_000_000_000 + self.index * 5_000_000_000,
                         "total_bytes": 934_000_000_000},
                "gpu": {"name": "GPU-0 (synthetic)", "memory_total_mb": 0},
                "npus": [{"utilization_percent": 0.0, "memory_used_mb": used_mb,
                          "memory_total_mb": 49024, "name": "Houmo dNPU"}]}

    def device_info_payload(self):
        return {"device_name": self.name, "device_model_name": "Tiiny AI Pocket Lab",
                "device_model_version": "z01", "tiiny_os": "0.1.33",
                "version": "0.1.30", "sn": self.serial,
                "cpu": "12-core ARMv9.2", "gpu": "Mali-G720-Immortalis 10 cores",
                "peak_int8": "190 TOPS", "ram": "80 GB", "storage": "1 TB",
                "network": "wifi"}

    def device_json_payload(self, host_port=None):
        """Verified against live firmware on 2026-09-13.

        This was the largest unverified shape in this file and is now a
        recording: the field names, the discovery token, the USB /30 and the
        per-interface address list are all as the device sends them. The address
        list is what lets one box register once with both of its planes.
        """
        lan = self.lan_address or "192.168.100.94"
        usb = self.usb_address or "172.17.7.177"
        return {
            "schema_version": "1",
            "device_name": self.name,
            "device_id": self.device_id,
            "serial_number": self.serial,
            "hostname": "tiinyhost",
            "discovery_token": "GADGET_DISCOVER_V1",
            "transport": ["lan", "usb"],
            "service": {"instance_name": self.name, "dns_sd": "_gadget._tcp",
                        "http_port": 39218, "http_path": "/device.json",
                        "udp_discovery_port": 39217},
            "backend": {"scheme": "http", "port": 0, "path": "/"},
            "usb": {"interface": "usb0", "active": 1,
                    "network": "172.17.7.176/30", "device_ip": usb,
                    "host_ip": "172.17.7.178",
                    "sn_derived_link_local_ipv6": "fe80::52ff:e2c5:f7a5:6d86",
                    "link_local_ipv6": "fe80::52ff:e2c5:f7a5:6d86",
                    "ipv6_addresses": ["fe80::52ff:e2c5:f7a5:6d86"],
                    "device_mac": "02:ce:28:81:f3:01",
                    "host_mac": "02:ce:28:81:f3:02"},
            "ipv4_addresses": [{"interface": "usb0", "address": usb},
                               {"interface": "wlan0", "address": lan}],
            "ipv6_addresses": [
                {"interface": "usb0", "address": "fe80::52ff:e2c5:f7a5:6d86",
                 "scope": "link"},
                {"interface": "wlan0",
                 "address": "fd23:2dd7:c811:4229:e345:4f03:1396:fa37",
                 "scope": "global"}]}

    def catalog_payload(self):
        out = []
        for row in CATALOG:
            short = row["model_id"].split("/")[-1]
            out.append({"model_id": row["model_id"], "id": row["model_id"],
                        "name": short, "fullname": row["model_id"],
                        "display_name": short, "type": row["type"],
                        "params": row["params"], "size": row["size"],
                        "npu_usage": row["npu_usage"],
                        "capabilities": list(row.get("capabilities") or []),
                        "status": "downloaded" if row["model_id"] in self.installed
                                  else "not_downloaded"})
        return out

    def storage_payload(self):
        """Verified 2026-09-13. This one was wrong.

        The real response wraps everything in a success/data envelope, which the
        earlier inferred version did not have. Nothing in Pocket reads this
        endpoint, which is exactly why the mistake could sit here unnoticed, and
        why checking it against hardware was worth doing.
        """
        models = [{"model_id": m, "size_bytes": self._row(m)["size"],
                   "size_human": "%.2f GB" % (self._row(m)["size"] / 1e9),
                   "model_path": "/data/models/%s" % m, "progress": 100.0,
                   "error": None} for m in self.installed]
        total = sum(m["size_bytes"] for m in models)
        return {"success": True,
                "data": {"total_size_bytes": total,
                         "total_size_human": "%.2f GB" % (total / 1e9),
                         "total_unique_size_bytes": total,
                         "total_shared_size_bytes": 0,
                         "model_count": len(models), "models": models}}


class FakeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "tiiny-fake/1.0"

    @property
    def state(self):
        return self.server.state

    def log_message(self, *args):
        pass

    # ------------------------------------------------------------- plumbing
    def _send(self, status, payload, headers=None):
        blob = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(blob)

    def _authed(self):
        header = self.headers.get("Authorization", "")
        if header == "Bearer " + KEY:
            return True
        self._send(401, {"code": 401, "msg": "Not authenticated"})
        return False

    # Paths the device management plane serves on port 80 with no Host header.
    MGMT_PREFIXES = ("/device.json", "/api/v1/sys/", "/health")

    def _routed(self, path):
        """Is this request addressed to the surface that serves this path?

        Only enforced when the fake is in vhost_only mode. A gateway path then
        needs the Host header the vendor CLI sends, and without one port 80
        answers as the management UI, which is what the real device does.
        """
        if not self.state.vhost_only:
            return True
        if path.startswith(self.MGMT_PREFIXES):
            return True
        host = (self.headers.get("Host") or "").split(":")[0]
        wanted = OPENAI_VHOST if path.startswith("/v1") else GATEWAY_VHOST
        if host == wanted:
            self.state.vhost_hits += 1
            return True
        return False

    def _unrouted(self, path):
        """What port 80 says to a gateway path with no usable Host header."""
        if path == "/":
            return self._send(200, {"service": "device management",
                                    "device_name": self.state.name})
        return self._send(404, {"code": 404, "msg": "Not Found",
                                "detail": "no route for %s on this virtual host" % path})

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except ValueError:
            return {}

    def _model_from(self, path, prefix, suffix=""):
        """Pull a URL-encoded model id out of a path.

        A fake that quietly accepted an unencoded slash would hide the single
        most common real bug, so an id arriving with a raw slash is rejected.
        """
        rest = path[len(prefix):]
        if suffix:
            if not rest.endswith(suffix):
                return None
            rest = rest[:-len(suffix)]
        if not rest or "/" in rest:
            return None
        return urllib.parse.unquote(rest)

    # ------------------------------------------------------------------- GET
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        state = self.state
        if not self._routed(path):
            return self._unrouted(path)

        if path == "/device.json":
            return self._send(200, state.device_json_payload())
        if path == "/api/v1/sys/device_info":
            return self._send(200, state.device_info_payload())
        if path == "/health":
            return self._send(200, {"status": "ok"})

        if not self._authed():
            return None

        if path == "/api/v1/sys/status":
            return self._send(200, state.sys_status_payload())
        if path == "/api/v1/models/":
            return self._send(200, state.models_payload())
        if path == "/api/v1/models/running":
            return self._send(200, state.running_payload())
        if path == "/api/v1/models/npu/status":
            return self._send(200, state.units_payload())
        if path == "/api/v1/npu/status":
            return self._send(200, state.npu_status_payload())
        if path == "/api/v1/models/online_models":
            return self._send(200, state.catalog_payload())
        if path == "/api/v1/models/storage":
            return self._send(200, state.storage_payload())
        if path == "/v1/models":
            data = [{"id": m, "object": "model", "created": 0,
                     "owned_by": state.name} for m in state.loaded]
            return self._send(200, {"object": "list", "data": data})

        if path.startswith("/api/v1/models/") and path.endswith("/get_progress"):
            model_id = self._model_from(path, "/api/v1/models/", "/get_progress")
            if model_id is None:
                return self._send(404, {"code": 404, "msg": "Not Found"})
            return self._send(200, self._progress(model_id))

        return self._send(404, {"code": 404, "msg": "Not Found"})

    def _progress(self, model_id):
        # Verified 2026-09-13 for a model already on disk: the device answers
        # {model_id, fullname, status, progress}. The downloading case is still
        # inferred, because confirming it means starting a download.
        state = self.state
        with state.guard:
            job = state.downloads.get(model_id)
            if job is None:
                if model_id in state.installed:
                    return {"model_id": model_id, "status": "downloaded",
                            "progress": 100.0}
                return {"model_id": model_id, "status": "not_downloaded",
                        "progress": 0.0}
            job["progress"] = min(100.0, job["progress"] + 25.0)
            if job["progress"] >= 100.0:
                if model_id not in state.installed:
                    state.installed.append(model_id)
                state.downloads.pop(model_id, None)
                return {"model_id": model_id, "status": "downloaded",
                        "progress": 100.0}
            return {"model_id": model_id, "status": "downloading",
                    "progress": job["progress"],
                    "speed_human": "42.0 MB/s"}

    # ------------------------------------------------------------------ POST
    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if not self._routed(path):
            return self._unrouted(path)
        if not self._authed():
            return None
        state = self.state

        if path == "/v1/chat/completions":
            return self._chat(self._body())
        if path == "/api/v1/models/unload_all":
            with state.guard:
                state.loaded = []
            return self._send(200, {"removed_container_ids": []})

        prefix = "/api/v1/models/"
        for suffix, handler in (("/start", self._start), ("/stop", self._stop),
                                ("/download", self._download),
                                ("/download/stream", self._download_stream)):
            if path.startswith(prefix) and path.endswith(suffix):
                model_id = self._model_from(path, prefix, suffix)
                if model_id is None:
                    return self._send(404, {"code": 404, "msg": "Not Found"})
                return handler(model_id)

        return self._send(404, {"code": 404, "msg": "Not Found"})

    def do_DELETE(self):
        path = urllib.parse.urlsplit(self.path).path
        if not self._routed(path):
            return self._unrouted(path)
        if not self._authed():
            return None
        state = self.state
        model_id = self._model_from(path, "/api/v1/models/")
        if model_id is None:
            return self._send(404, {"code": 404, "msg": "Not Found"})
        with state.guard:
            if model_id in state.loaded:
                return self._send(409, {"code": 409, "msg": "Model delete is blocked.",
                                        "detail": "model is loaded; stop it first"})
            if model_id not in state.installed:
                return self._send(400, {"code": 400, "msg": "Error deleting model.",
                                        "detail": "not installed"})
            state.installed.remove(model_id)
        return self._send(200, {"message": "deleted %s" % model_id})

    # -------------------------------------------------------------- lifecycle
    def _start(self, model_id):
        state = self.state
        with state.guard:
            if model_id not in state.installed:
                return self._send(400, {"code": 400, "msg": "Error starting model.",
                                        "detail": "%s is not downloaded" % model_id})
            if model_id in state.loaded:
                return self._send(200, {"message": "%s already running" % model_id,
                                        "progress": 100})
            cost = state._cost(model_id)
            if state.units_used() + cost > state.npu_total:
                return self._send(400, {
                    "code": 400, "msg": "Error starting model.",
                    "detail": "needs %d NPU units, only %d free"
                              % (cost, state.npu_total - state.units_used())})
            state.loaded.append(model_id)
        return self._send(200, {"message": "start loading %s" % model_id, "progress": 0})

    def _stop(self, model_id):
        state = self.state
        with state.guard:
            if model_id not in state.loaded:
                return self._send(400, {"code": 400, "msg": "Error stopping model.",
                                        "detail": "%s is not running" % model_id})
            state.loaded.remove(model_id)
        return self._send(200, {"removed_container_ids": ["fake%s" % abs(hash(model_id))]})

    def _download(self, model_id):
        state = self.state
        with state.guard:
            if model_id in state.installed:
                return self._send(200, {"message": "%s already downloaded" % model_id,
                                        "progress": 100})
            state.downloads[model_id] = {"progress": 0.0, "started": time.time()}
        return self._send(200, {"message": "start downloading %s" % model_id,
                                "progress": 0})

    def _download_stream(self, model_id):
        """The SSE sibling. Real firmware streams progress for up to six hours.

        UNVERIFIED: the frame body is the largest guess in this file. The spec
        documents only that the endpoint streams progress. These fields are the
        get_progress ones plus speed_human, which is a real OpenAIModel field.
        The browser reads progress defensively and falls back to showing the
        status text, so an unexpected shape shows less rather than breaking.
        """
        state = self.state
        with state.guard:
            already = model_id in state.installed
            if not already:
                state.downloads[model_id] = {"progress": 0.0, "started": time.time()}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            if already:
                self._sse({"model_id": model_id, "status": "downloaded",
                           "progress": 100.0})
            else:
                for step in range(1, 5):
                    self._sse({"model_id": model_id, "status": "downloading",
                               "progress": step * 25.0,
                               "speed_human": "42.0 MB/s"})
                    # Only pause when this fake is pacing itself for a demo, so
                    # the progress bar is actually watchable. Tests get it instantly.
                    if state.token_delay:
                        time.sleep(DEMO_DOWNLOAD_STEP)
                with state.guard:
                    if model_id not in state.installed:
                        state.installed.append(model_id)
                    state.downloads.pop(model_id, None)
                self._sse({"model_id": model_id, "status": "downloaded",
                           "progress": 100.0})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def _sse(self, payload):
        self.wfile.write(b"data: " + json.dumps(payload).encode() + b"\n\n")
        self.wfile.flush()

    # -------------------------------------------------------------- inference
    def _chat(self, body):
        state = self.state
        model_id = body.get("model")
        if model_id not in state.loaded:
            # The recorded not-loaded shape. Models never auto-load.
            return self._send(404, {"error": {
                "code": 404, "message": '"%s" is not loaded.' % model_id,
                "type": "model_not_found"}})

        if not state.inference.acquire(blocking=False):
            # One inference at a time. This is the collision every caller hits
            # and the whole reason Pocket holds a lock.
            with state.guard:
                state.collisions += 1
            return self._send(200, {"code": 150004,
                                    "message": "The operation failed to complete."})
        try:
            with state.guard:
                state.served += 1
            if body.get("stream"):
                return self._chat_stream(body, model_id)
            return self._chat_once(body, model_id)
        finally:
            state.inference.release()

    def _tokens(self, body):
        want = int(body.get("max_tokens") or 64)
        words = SAMPLE.split()
        return [words[i % len(words)] + " " for i in range(max(1, min(want, TOKEN_CAP)))]

    @staticmethod
    def _prompt_tokens(body):
        """Roughly four characters per token, counted off the real prompt.

        A fixed number here would make the prefill-scaling benchmark draw a flat
        line against prompts of wildly different lengths, which would look like
        a broken measurement rather than a fake one.
        """
        chars = 0
        for message in body.get("messages") or []:
            content = message.get("content")
            if isinstance(content, str):
                chars += len(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        chars += len(part["text"])
        return max(1, chars // 4)

    def _pace(self, started, index):
        """Hold token `index` back until its slot in the deadline arrives."""
        delay = self.state.token_delay
        if not delay:
            return
        owed = (started + index * delay) - time.time()
        if owed >= MIN_SLEEP:
            time.sleep(owed)

    def _chat_once(self, body, model_id):
        state = self.state
        tokens = self._tokens(body)
        started = time.time()
        for index in range(len(tokens)):
            self._pace(started, index + 1)
            if state.request_ceiling and time.time() - started > state.request_ceiling:
                # The gateway ceiling: measured at 222.3s on real firmware after
                # 580 tokens, with the client timeout still at 780s.
                self.send_response(504)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
                return None
        text = "".join(tokens).strip()
        elapsed = max(time.time() - started, 0.001)
        prompt_n = self._prompt_tokens(body)
        # API.md records ~28 tok/s prompt processing on real firmware.
        prompt_ms = round(prompt_n / PREFILL_TOK_S * 1000, 1)
        return self._send(200, {
            "id": "chatcmpl-fake-%d" % int(started),
            "object": "chat.completion", "created": int(started), "model": model_id,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": prompt_n, "completion_tokens": len(tokens),
                      "total_tokens": prompt_n + len(tokens),
                      "prompt_tokens_details": {"cached_tokens": 0}},
            # Real gateways return a timings block; tiiny-bench reads it.
            "timings": {"prompt_n": prompt_n, "prompt_ms": prompt_ms,
                        "prompt_per_second": PREFILL_TOK_S,
                        "predicted_n": len(tokens),
                        "predicted_per_second": round(len(tokens) / elapsed, 2),
                        "predicted_per_token_ms": round(elapsed * 1000 / len(tokens), 2)}})

    def _chat_stream(self, body, model_id):
        state = self.state
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        created = int(time.time())

        def frame(delta, finish=None):
            return {"id": "chatcmpl-fake-%d" % created, "object": "chat.completion.chunk",
                    "created": created, "model": model_id,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        try:
            self._sse(frame({"role": "assistant", "content": ""}))
            # Reasoning models split output: chain of thought lands in
            # reasoning_content and counts against max_tokens.
            self._sse(frame({"reasoning_content": "Checking the bandwidth math. "}))
            started = time.time()
            tokens = self._tokens(body)
            for index, token in enumerate(tokens):
                self._pace(started, index + 1)
                self._sse(frame({"content": token}))
            self._sse(frame({}, finish="stop"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True
        return None


class FakeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, state, host="127.0.0.1", port=0):
        self.state = state
        super().__init__((host, port), FakeHandler)


class FakeDevice:
    """A fake Tiiny on an ephemeral port. Use as a context manager or call
    start() and stop().

    `planes` starts one listener per addressing plane, all sharing one state, so
    a single fake box answers on several addresses at once exactly as real
    hardware does over USB and Wi-Fi together. The planes differ by port rather
    than by address because loopback aliases are not portable, and nothing in
    Pocket cares which of the two a plane's base URL varies by.
    """

    def __init__(self, index=1, host="127.0.0.1", port=0, planes=None, **kwargs):
        self.state = FakeState(index=index, **kwargs)
        self.host = host
        self.key = KEY
        self.threads = []
        names = list(planes or ["lan"])
        self.servers = {}
        self.bases = {}
        for offset, name in enumerate(names):
            server = FakeServer(self.state, host, port if offset == 0 else 0)
            self.servers[name] = server
            self.bases[name] = "http://%s:%d" % (host, server.server_address[1])
        self.plane_names = names
        self.server = self.servers[names[0]]
        self.port = self.server.server_address[1]
        self.base = self.bases[names[0]]

    def plane_records(self):
        """Plane dicts ready to hand to Fleet.register.

        A vhost-only fake points each plane's direct gateway at a closed port,
        so every plane has to fall back to the virtual host, which is what the
        firmware with port 8800 shut actually does.
        """
        out = []
        for name in self.plane_names:
            base = self.bases[name]
            # The port is what actually distinguishes one fake plane from
            # another, so it belongs in the address: two planes both reading
            # "127.0.0.1" would be indistinguishable on the device card.
            out.append({"name": name,
                        "address": "%s:%d" % (self.host,
                                              self.servers[name].server_address[1]),
                        "interface": "usb0" if name == "usb" else "wlan0",
                        "gateway": self.gateway_base_for(name), "mgmt": base,
                        "discovery": base, "vhost_base": base})
        return out

    def gateway_base_for(self, name):
        base = self.bases[name]
        if not self.state.vhost_only:
            return base
        key = "_closed_" + name
        if getattr(self, key, None) is None:
            setattr(self, key, closed_port(self.host))
        return "http://%s:%d" % (self.host, getattr(self, key))

    def stop_plane(self, name):
        """Pull one plane's cable."""
        server = self.servers.pop(name, None)
        if server is None:
            return
        server.shutdown()
        server.server_close()
        self.plane_names = [n for n in self.plane_names if n != name]

    @property
    def vhost_only(self):
        return self.state.vhost_only

    @property
    def gateway_base(self):
        """What to hand Pocket as the direct gateway URL for the first plane."""
        return self.gateway_base_for(self.plane_names[0])

    @property
    def serial(self):
        return self.state.serial

    @property
    def name(self):
        return self.state.name

    def start(self):
        for server in self.servers.values():
            thread = threading.Thread(target=server.serve_forever,
                                      kwargs={"poll_interval": 0.1}, daemon=True)
            thread.start()
            self.threads.append(thread)
        return self

    def stop(self):
        for server in list(self.servers.values()):
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
        for thread in self.threads:
            thread.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


class FakeDiscovery:
    """The UDP responder device.json advertises.

    Send it the discovery token and it answers with the whole device.json, which
    is exactly what live firmware does on port 39217. Bound to an ephemeral port
    here so a test never fights with the real one.
    """

    def __init__(self, state, host="127.0.0.1", port=0):
        self.state = state
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.port = self.sock.getsockname()[1]
        self.host = host
        self.running = False
        self.thread = None

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        return self

    def _serve(self):
        self.sock.settimeout(0.2)
        while self.running:
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if data.strip() != b"GADGET_DISCOVER_V1":
                continue
            try:
                self.sock.sendto(
                    json.dumps(self.state.device_json_payload()).encode(), addr)
            except OSError:
                break

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass
        if self.thread is not None:
            self.thread.join(timeout=3)


def closed_port(host="127.0.0.1"):
    """A port nothing is listening on, so connecting to it is refused.

    Bind it, read the number the kernel picked, drop it. Something else could
    claim it in the gap, which is why this is only used by tests: a stray
    listener would make the refused-transport test fail loudly rather than pass
    for the wrong reason.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, 0))
        return probe.getsockname()[1]
    finally:
        probe.close()


def fleet_of(count, planes_for_first=None, **kwargs):
    """Start `count` fake devices, each with a different mix of models loaded so
    the routing and the union in /v1/models have something to do.

    planes_for_first gives the first box more than one addressing plane, which
    is what a real Tiiny looks like when it is plugged in over USB and joined to
    Wi-Fi at the same time.
    """
    # The second box carries the mix a real device ends up with: a chat model
    # loaded next to an embedding and a text-to-speech model, none of which can
    # answer a chat completion. The third has only a non-chat model loaded,
    # which is the case where the Chat picker must offer nothing rather than
    # offer an image generator.
    spreads = [
        (DEFAULT_INSTALLED, ["deepreinforce-ai/Ornith-1.0-35B"]),
        (["Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo", "Qwen/Qwen3-Embedding-0.6B",
          "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", "zai-org/GLM-4.7-Flash"],
         ["Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo", "Qwen/Qwen3-Embedding-0.6B",
          "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"]),
        (["deepreinforce-ai/Ornith-1.0-35B", "Tongyi-MAI/Z-Image-Turbo"],
         ["Tongyi-MAI/Z-Image-Turbo"]),
    ]
    devices = []
    for index in range(count):
        installed, loaded = spreads[index % len(spreads)]
        devices.append(FakeDevice(index=index + 1, installed=list(installed),
                                  loaded=list(loaded),
                                  planes=planes_for_first if index == 0 else None,
                                  **kwargs).start())
    return devices
