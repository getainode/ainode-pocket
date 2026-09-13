"""A fake Tiiny, good enough to develop and test against with no hardware.

Every response shape here comes from a recorded artefact, not from guesswork:

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
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY = "00000000-0000-4000-8000-000000000000"

# Unit costs are the measured ones from CAPABILITIES.md.
CATALOG = [
    {"model_id": "deepreinforce-ai/Ornith-1.0-35B", "type": "Image-Text-to-Text",
     "params": "35B", "size": 18_000_000_000, "npu_usage": 50},
    {"model_id": "Qwen/Qwen3.6-35B-A3B", "type": "Image-Text-to-Text",
     "params": "35B-A3B", "size": 18_000_000_000, "npu_usage": 50},
    {"model_id": "Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo", "type": "Text Generation",
     "params": "30B-A3B", "size": 15_200_000_000, "npu_usage": 45},
    {"model_id": "Tongyi-MAI/Z-Image-Turbo", "type": "Text-to-Image",
     "params": "6B", "size": 10_200_000_000, "npu_usage": 32},
    {"model_id": "Qwen/Qwen3-ASR-1.7B", "type": "ASR",
     "params": "1.7B", "size": 3_600_000_000, "npu_usage": 7},
    {"model_id": "Qwen/Qwen3-TTS-12Hz-1.7B-Base", "type": "Text-to-Speech",
     "params": "1.7B", "size": 2_500_000_000, "npu_usage": 5},
    {"model_id": "Qwen/Qwen3-Reranker-0.6B", "type": "Text Reranking",
     "params": "0.6B", "size": 700_000_000, "npu_usage": 2},
    {"model_id": "Qwen/Qwen3-Embedding-0.6B", "type": "Text Embedding",
     "params": "0.6B", "size": 900_000_000, "npu_usage": 1},
    {"model_id": "openai/gpt-oss-20b", "type": "Text Generation",
     "params": "20B", "size": 12_000_000_000, "npu_usage": 30},
    {"model_id": "zai-org/GLM-4.7-Flash", "type": "Text Generation",
     "params": "9B", "size": 6_000_000_000, "npu_usage": 12},
]

DEFAULT_INSTALLED = ["deepreinforce-ai/Ornith-1.0-35B",
                     "Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo",
                     "Qwen/Qwen3-Embedding-0.6B"]

SAMPLE = ("Memory bandwidth sets the ceiling here. The accelerator reads the "
          "active weights once per token, so tokens per second is bandwidth "
          "divided by bytes read, and nothing in the scheduler changes that. ")


class FakeState:
    """One fake device's mutable state, guarded for concurrent handlers."""

    def __init__(self, index=1, installed=None, loaded=None, npu_total=100,
                 token_delay=0.0, request_ceiling=None, serial=None):
        self.index = index
        self.serial = serial or "TNYF260900000000%02dQ" % index
        self.name = "tiiny-fake-%d" % index
        self.npu_total = npu_total
        self.token_delay = token_delay
        # Scaled stand-in for the measured ~222s gateway ceiling.
        self.request_ceiling = request_ceiling
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
                "size": 0, "npu_usage": 1}

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
                "model_id": model_id, "display_name": short,
                "hf_repo_id": model_id, "object": "model", "created": 0,
                "owned_by": "Model store", "status": "downloaded",
                "download_status": "downloaded", "progress": 100.0,
                "npu_usage": row["npu_usage"]})
        return {"object": "list", "data": data}

    def running_payload(self):
        instances = []
        for offset, model_id in enumerate(self.loaded):
            instances.append({"model_id": model_id, "port": 9098 + offset,
                              "npu_usage": self._cost(model_id),
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

    def device_json_payload(self, host_port):
        return {"device_name": self.name, "sn": self.serial,
                "device_model": "Tiiny AI Pocket Lab", "tiiny_os": "0.1.33",
                "addresses": {"lan": host_port, "usb": "172.17.7.177"},
                "services": {"gateway": 8800, "management": 80, "discovery": 39218}}

    def catalog_payload(self):
        out = []
        for row in CATALOG:
            short = row["model_id"].split("/")[-1]
            out.append({"model_id": row["model_id"], "id": row["model_id"],
                        "name": short, "fullname": row["model_id"],
                        "display_name": short, "type": row["type"],
                        "params": row["params"], "size": row["size"],
                        "npu_usage": row["npu_usage"],
                        "status": "downloaded" if row["model_id"] in self.installed
                                  else "not_downloaded"})
        return out

    def storage_payload(self):
        models = [{"model_id": m, "size_bytes": self._row(m)["size"],
                   "model_path": "/data/models/%s" % m, "progress": 100.0,
                   "error": None} for m in self.installed]
        return {"total_size_bytes": sum(m["size_bytes"] for m in models),
                "model_count": len(models), "models": models}


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

        if path == "/device.json":
            host = self.headers.get("Host") or "127.0.0.1"
            return self._send(200, state.device_json_payload(host))
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
        """The SSE sibling. Real firmware streams progress for up to six hours."""
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
                    time.sleep(state.token_delay)
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
        return [words[i % len(words)] + " " for i in range(max(1, min(want, 96)))]

    def _chat_once(self, body, model_id):
        state = self.state
        tokens = self._tokens(body)
        started = time.time()
        for _ in tokens:
            if state.token_delay:
                time.sleep(state.token_delay)
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
        return self._send(200, {
            "id": "chatcmpl-fake-%d" % int(started),
            "object": "chat.completion", "created": int(started), "model": model_id,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 24, "completion_tokens": len(tokens),
                      "total_tokens": 24 + len(tokens),
                      "prompt_tokens_details": {"cached_tokens": 0}},
            # Real gateways return a timings block; tiiny-bench reads it.
            "timings": {"prompt_n": 24, "prompt_ms": 850.0,
                        "prompt_per_second": 28.2,
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
            for token in self._tokens(body):
                if state.token_delay:
                    time.sleep(state.token_delay)
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
    start() and stop()."""

    def __init__(self, index=1, host="127.0.0.1", port=0, **kwargs):
        self.state = FakeState(index=index, **kwargs)
        self.server = FakeServer(self.state, host, port)
        self.port = self.server.server_address[1]
        self.host = host
        self.base = "http://%s:%d" % (host, self.port)
        self.key = KEY
        self.thread = None

    @property
    def serial(self):
        return self.state.serial

    @property
    def name(self):
        return self.state.name

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


def fleet_of(count, **kwargs):
    """Start `count` fake devices, each with a different mix of models loaded so
    the routing and the union in /v1/models have something to do."""
    spreads = [
        (DEFAULT_INSTALLED, ["deepreinforce-ai/Ornith-1.0-35B"]),
        (["Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo", "Qwen/Qwen3-Embedding-0.6B",
          "zai-org/GLM-4.7-Flash"],
         ["Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo", "Qwen/Qwen3-Embedding-0.6B"]),
        (["deepreinforce-ai/Ornith-1.0-35B", "Tongyi-MAI/Z-Image-Turbo"],
         ["Tongyi-MAI/Z-Image-Turbo"]),
    ]
    devices = []
    for index in range(count):
        installed, loaded = spreads[index % len(spreads)]
        devices.append(FakeDevice(index=index + 1, installed=list(installed),
                                  loaded=list(loaded), **kwargs).start())
    return devices
