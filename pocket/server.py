"""The local web app: one HTTP server, the UI, the JSON API, and the endpoint.

Nothing here needs a framework. The whole app is one ThreadingHTTPServer with a
route table, which is what lets Pocket ship as a farm app with no pip
dependencies at all.
"""
from __future__ import annotations

import json
import os
import posixpath
import threading
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import bench as bench_mod
from . import device as device_mod
from . import gateway
from .fleet import Fleet, NoDevice

VERSION = "0.1.2"
PORT = 8430
WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")

CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json"}

FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="12" fill="#0a0a0a"/>'
    '<rect x="14" y="14" width="36" height="36" rx="8" fill="none" '
    'stroke="#76B900" stroke-width="5"/>'
    '<rect x="27" y="27" width="10" height="10" rx="2" fill="#76B900"/>'
    '</svg>')


class App:
    """Shared state: the fleet, the current benchmark run, the served port."""

    def __init__(self, fleet=None, fakes=None):
        self.fleet = fleet if fleet is not None else Fleet()
        self.fakes = fakes or []
        self.bench_run = None
        self.guard = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ainode-pocket/" + VERSION

    @property
    def app(self):
        return self.server.app

    @property
    def fleet(self):
        return self.server.app.fleet

    def log_message(self, fmt, *args):
        if os.environ.get("AINODE_POCKET_VERBOSE"):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------- plumbing
    def send_json(self, status, payload):
        blob = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(blob)

    def send_text(self, status, body, ctype="text/plain; charset=utf-8"):
        blob = body if isinstance(body, bytes) else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(blob)

    def begin_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def write_chunk(self, blob):
        self.wfile.write(blob)
        self.wfile.flush()

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8", "replace"))
        except ValueError:
            return None

    def query(self):
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

    def one(self, name, default=None):
        return (self.query().get(name) or [default])[0]

    # ------------------------------------------------------------------ GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/healthz":
                return self.send_json(200, {"ok": True, "version": VERSION,
                                            "devices": len(self.fleet.devices)})
            if path == "/favicon.svg" or path == "/favicon.ico":
                return self.send_text(200, FAVICON, "image/svg+xml")
            if path in ("/", "/index.html"):
                return self.static("index.html")
            if path in ("/app.css", "/app.js"):
                return self.static(path.lstrip("/"))
            if path == "/v1/models":
                loaded_only = self.one("loaded") in ("1", "true", "yes")
                return self.send_json(200, gateway.models_payload(self.fleet, loaded_only))
            if path.startswith("/api/"):
                return self.api_get(path)
            return self.send_json(404, {"error": {"message": "no route for " + path}})
        except BrokenPipeError:
            return None
        except Exception as exc:
            traceback.print_exc()
            return self.send_json(500, {"error": {"message": str(exc)}})

    def static(self, name):
        safe = posixpath.normpath("/" + name).lstrip("/")
        target = os.path.join(WEB_DIR, safe)
        if not os.path.abspath(target).startswith(os.path.abspath(WEB_DIR)) \
                or not os.path.isfile(target):
            return self.send_json(404, {"error": {"message": "missing asset " + name}})
        with open(target, "rb") as handle:
            blob = handle.read()
        ctype = CONTENT_TYPES.get(os.path.splitext(target)[1], "application/octet-stream")
        return self.send_text(200, blob, ctype)

    def api_get(self, path):
        if path == "/api/state":
            return self.send_json(200, self.state())
        if path == "/api/models":
            device_id = self.one("device")
            force = self.one("force") in ("1", "true")
            if device_id:
                try:
                    return self.send_json(200, {"device": device_id,
                                                "models": self.fleet.models(device_id, force)})
                except (NoDevice, device_mod.DeviceError) as exc:
                    return self.send_json(502, {"error": {"message": str(exc)}})
            index = self.fleet.index(force)
            return self.send_json(200, {"models": [index[k] for k in sorted(index)]})
        if path == "/api/catalog":
            device_id = self.one("device")
            try:
                dev = self.fleet.get(device_id)
                installed = {row["model_id"] for row in self.fleet.models(device_id)}
                rows = []
                for entry in dev.catalog():
                    if not isinstance(entry, dict):
                        continue
                    model_id = entry.get("model_id") or entry.get("id") or entry.get("name")
                    if not model_id:
                        continue
                    rows.append({"model_id": model_id,
                                 "name": entry.get("display_name")
                                         or entry.get("name") or model_id,
                                 "type": entry.get("type") or "",
                                 "params": entry.get("params") or "",
                                 "size": entry.get("size") or 0,
                                 "npu_usage": entry.get("npu_usage") or 0,
                                 "installed": model_id in installed})
                rows.sort(key=lambda row: (row["type"], row["model_id"]))
                return self.send_json(200, {"device": device_id, "catalog": rows})
            except (NoDevice, device_mod.DeviceError) as exc:
                return self.send_json(502, {"error": {"message": str(exc)}})
        if path == "/api/models/progress":
            device_id, model_id = self.one("device"), self.one("model")
            try:
                dev = self.fleet.get(device_id)
                return self.send_json(200, dev.progress(model_id))
            except (NoDevice, device_mod.DeviceError) as exc:
                return self.send_json(502, {"error": {"message": str(exc)}})
        if path == "/api/models/events":
            return self.download_events()
        if path == "/api/bench":
            with self.app.guard:
                run = self.app.bench_run
            return self.send_json(200, {"history": bench_mod.history(),
                                        "current": run.snapshot() if run else None})
        if path == "/api/bench/result":
            try:
                return self.send_json(200, bench_mod.load(self.one("name") or ""))
            except Exception as exc:
                return self.send_json(404, {"error": {"message": str(exc)}})
        return self.send_json(404, {"error": {"message": "no route for " + path}})

    def state(self):
        """Everything the UI paints, in one call."""
        devices = []
        for device_id in list(self.fleet.devices):
            try:
                devices.append(self.fleet.telemetry(device_id))
            except NoDevice:
                continue
        index = self.fleet.index()
        ready = sum(1 for slot in index.values() if slot["loaded_on"])
        endpoint_host = self.headers.get("Host") or ("127.0.0.1:%d" % PORT)
        return {"version": VERSION,
                "devices": devices,
                # What the Chat page is allowed to offer. A device holds speech,
                # embedding and image models too and the page must not put one
                # in the picker, so the answer is computed here rather than
                # guessed from the running list in the browser.
                "chat_models": self.chat_models(index),
                "summary": {"devices": len(devices),
                            "online": sum(1 for d in devices if d.get("online")),
                            "models": len(index),
                            "models_ready": ready,
                            "chat_ready": sum(1 for slot in index.values()
                                              if slot["chat"] and slot["loaded_on"]),
                            "loaded": sum(len(d.get("running") or []) for d in devices)},
                "endpoint": {"base_url": "http://%s/v1" % endpoint_host,
                             "models": "http://%s/v1/models" % endpoint_host},
                "fake": [{"name": f.name, "base": f.base} for f in self.app.fakes]}

    def chat_models(self, index):
        """Loaded chat models, each labelled with where it is loaded.

        The endpoint routes on the model id alone, so this is one row per model
        and not one per copy: naming two devices on a model loaded on both would
        suggest a choice the caller does not get to make.
        """
        rows = []
        for model_id in sorted(index):
            slot = index[model_id]
            if not slot["chat"] or not slot["loaded_on"]:
                continue
            names = [self.fleet.devices[d].name for d in slot["loaded_on"]
                     if d in self.fleet.devices]
            rows.append({"model_id": model_id,
                         "type": slot["type"],
                         "devices": slot["loaded_on"],
                         "where": names[0] if len(names) == 1
                                  else "%d devices" % len(names)})
        return rows

    def download_events(self):
        """Proxy the device's download SSE straight through to the browser."""
        device_id, model_id = self.one("device"), self.one("model")
        try:
            dev = self.fleet.get(device_id)
        except NoDevice as exc:
            return self.send_json(404, {"error": {"message": str(exc)}})
        self.begin_sse()
        try:
            for line in dev.download_stream(model_id):
                if line.strip():
                    self.write_chunk((line + "\n\n").encode())
            self.fleet.invalidate(device_id)
            self.write_chunk(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except device_mod.DeviceError as exc:
            try:
                self.write_chunk(("data: %s\n\n"
                                  % json.dumps({"error": str(exc)})).encode())
            except OSError:
                pass
        return None

    # ----------------------------------------------------------------- POST
    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        try:
            payload = self.body()
            if payload is None:
                return self.send_json(400, {"error": {"message": "body must be JSON"}})
            if path == "/v1/chat/completions":
                return self.chat(payload)
            if path == "/api/devices":
                return self.add_device(payload)
            if path == "/api/discover":
                return self.discover(payload)
            if path in ("/api/models/load", "/api/models/unload",
                        "/api/models/delete", "/api/models/download"):
                return self.model_action(path.rsplit("/", 1)[1], payload)
            if path == "/api/bench":
                return self.start_bench(payload)
            return self.send_json(404, {"error": {"message": "no route for " + path}})
        except BrokenPipeError:
            return None
        except Exception as exc:
            traceback.print_exc()
            return self.send_json(500, {"error": {"message": str(exc)}})

    def do_DELETE(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/devices":
            device_id = self.one("id")
            removed = self.fleet.forget(device_id)
            return self.send_json(200 if removed else 404, {"removed": removed})
        return self.send_json(404, {"error": {"message": "no route for " + path}})

    def chat(self, payload):
        if payload.get("stream"):
            status, error, stream = gateway.chat_stream(self.fleet, payload)
            if stream is None:
                return self.send_json(status, error)
            self.begin_sse()
            try:
                for blob in stream:
                    self.write_chunk(blob)
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.fleet.invalidate()
            return None
        status, body = gateway.chat(self.fleet, payload)
        self.fleet.invalidate()
        return self.send_json(status, body)

    def add_device(self, payload):
        address = (payload.get("address") or "").strip()
        gateway_url = (payload.get("gateway") or "").strip()
        if not address and not gateway_url:
            return self.send_json(400, {"error": {"message": "address is required"}})
        key = (payload.get("key") or "").strip()
        if not key:
            # Never ask the user to paste a key we can find ourselves, and never
            # keep one anywhere but their own data directory. Candidates are
            # tried against the device, the way tiiny-bench does, because the
            # local storage holds several UUIDs and only one is live.
            key = device_mod.find_key(
                verify=device_mod.key_checker(address, gateway_url or None))
        try:
            dev = self.fleet.register(address, key=key or None,
                                     name=payload.get("name"),
                                     gateway=gateway_url or None,
                                     mgmt=payload.get("mgmt") or None,
                                     discovery=payload.get("discovery") or None,
                                     planes=payload.get("planes") or None)
        except Exception as exc:
            return self.send_json(400, {"error": {"message": str(exc)}})
        probe = {"online": False, "error": None}
        try:
            probe = self.fleet.telemetry(dev.id, force=True)
        except Exception as exc:
            probe = {"online": False, "error": str(exc)}
        return self.send_json(200, {"device": {"id": dev.id, "name": dev.name,
                                              "address": dev.address,
                                              "addresses": dev.addresses,
                                              "route": dev.route_label},
                                    "telemetry": probe})

    def discover(self, payload):
        """Find devices.

        With no address this is the automatic sweep: a UDP broadcast plus the
        point-to-point links this host is plugged into. One box answering on
        both planes comes back once, with both addresses.
        """
        address = (payload.get("address") or "").strip()
        subnet = (payload.get("subnet") or "").strip()
        auto = bool(payload.get("auto"))
        if not (address or subnet or auto):
            return self.send_json(400, {"error": {
                "message": "give an address or a subnet, or auto for a broadcast "
                           "sweep of this network and any USB link"}})
        if address:
            found = device_mod.probe(address)
            if not found:
                return self.send_json(404, {"error": {
                    "message": "nothing answered http://%s:%d/device.json"
                               % (address, device_mod.DISCOVERY_PORT)}})
            records = [{"serial": device_mod.identity(found, address),
                        "name": found.get("device_name") or address,
                        "device": found, "seen_at": [address],
                        "planes": device_mod.planes_from(found, address)}]
        else:
            try:
                records = device_mod.discover(subnet=subnet or None)
            except ValueError as exc:
                return self.send_json(400, {"error": {"message": str(exc)}})
        out = []
        for record in records:
            planes = [p.as_dict() for p in record["planes"]]
            out.append({"serial": record["serial"], "name": record["name"],
                        "addresses": {p["name"]: p["address"] for p in planes},
                        "planes": planes, "seen_at": record.get("seen_at", []),
                        "address": planes[0]["address"] if planes else None,
                        "known": record["serial"] in self.fleet.devices,
                        "device": record["device"]})
        return self.send_json(200, {"found": out})

    def model_action(self, action, payload):
        device_id = payload.get("device")
        model_id = payload.get("model")
        if not device_id or not model_id:
            return self.send_json(400, {"error": {"message": "device and model required"}})
        try:
            dev = self.fleet.get(device_id)
            if action == "load":
                result = dev.start(model_id)
            elif action == "unload":
                result = dev.stop(model_id)
            elif action == "delete":
                result = dev.delete(model_id)
            else:
                result = dev.download(model_id)
        except NoDevice as exc:
            return self.send_json(404, {"error": {"message": str(exc)}})
        except device_mod.DeviceError as exc:
            return self.send_json(502, {"error": {"message": str(exc),
                                                  "code": exc.code}})
        self.fleet.invalidate(device_id)
        return self.send_json(200, {"result": result})

    def start_bench(self, payload):
        device_id = payload.get("device")
        if not device_id:
            return self.send_json(400, {"error": {"message": "device required"}})
        only = payload.get("only") or None
        if isinstance(only, str):
            only = [part.strip() for part in only.split(",") if part.strip()]
        model = (payload.get("model") or "").strip() or None
        # Every section of the suite is a chat completion, so a model that
        # cannot chat fails all of them and saves a result full of 400s. Refuse
        # in the same words the endpoint uses, before a single section runs.
        if model:
            refusal = gateway.refuse_non_chat(self.fleet, model)
            if refusal:
                return self.send_json(refusal[0], refusal[1])
            bad = self.model_not_loaded_here(device_id, model)
            if bad:
                return self.send_json(400, bad)
        with self.app.guard:
            current = self.app.bench_run
            if current is not None and current.state == "running":
                return self.send_json(409, {"error": {
                    "message": "a benchmark is already running"},
                    "current": current.snapshot()})
            run = bench_mod.start(self.fleet, device_id,
                                  payload.get("label") or "run", only, model)
            self.app.bench_run = run
        return self.send_json(200, {"current": run.snapshot()})

    def model_not_loaded_here(self, device_id, model):
        """The benchmark runs on one named box, so the model has to be on it.

        Nothing auto-loads on this hardware and the suite never loads anything,
        so a model loaded on a different device is a mistake worth naming.
        """
        slot = self.fleet.index().get(model)
        if slot is None:
            return {"error": {"message": "no device in this fleet has a model "
                                         "called %r" % model,
                              "type": "invalid_request_error", "code": 400}}
        if device_id in slot["loaded_on"]:
            return None
        try:
            name = self.fleet.get(device_id).name
        except NoDevice:
            name = device_id
        elsewhere = ", ".join(slot["loaded_on"])
        return {"error": {
            "message": "%s is not loaded on %s. The benchmark runs against what "
                       "is already loaded and never loads anything itself.%s"
                       % (model, name,
                          (" It is loaded on %s." % elsewhere) if elsewhere else ""),
            "type": "invalid_request_error", "code": 400}}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, app, host="0.0.0.0", port=PORT):
        self.app = app
        super().__init__((host, port), Handler)


def serve(app, host="0.0.0.0", port=PORT):
    server = Server(app, host, port)
    return server
