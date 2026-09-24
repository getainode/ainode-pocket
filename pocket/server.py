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
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import bench as bench_mod
from . import device as device_mod
from . import gateway
from .fleet import Fleet, NoDevice

VERSION = "0.1.4"
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


# What the Chat page asks for when it is not told otherwise. A reasoning model
# spends this budget on its chain of thought before the answer, so a small
# number here is how you get an empty reply and a "length" stop reason.
DEFAULT_MAX_TOKENS = 700

# The last line of a relayed stream. Pocket's own stats event goes in front of
# it, never after: every SSE client stops reading at [DONE], and one that stopped
# would never see the numbers.
DONE_LINE = b"data: [DONE]"


def chat_stats(timings, usage, total_ms, ttft_ms, finish_reason, device, model):
    """The one object the Chat page reads its numbers out of.

    Everything the device can measure comes from the device's own timings and
    usage blocks through the benchmark's derivation, so a number in the chat bar
    and the same number in a saved benchmark mean the same thing. Only the two
    wall clock figures belong to this machine: when the first token arrived and
    how long the whole request took, neither of which the device can see.

    A block the device never sent reports null here, not zero. The benchmark's
    derivation answers a missing block with zeroes because a saved row wants a
    number in every column, but on this page a zero is a claim: a stream that
    died at the 220 second cap never reaches the chunk carrying these blocks,
    and "out 0" beside a turn that really streamed four hundred tokens is the
    one kind of lie a release about trustworthy numbers cannot tell. The page
    prints a dash for null, which says the device did not report it.
    """
    derived = bench_mod.derive_stats(timings, usage)
    measured, counted = bool(timings), bool(usage) or bool(timings)
    if ttft_ms is None and measured:
        # Nothing streamed, so there was no first token to time here. The
        # device's own answer is prefill plus one token of decode, which is what
        # the benchmark reports for the same request.
        ttft_ms = round(derived["ttft_s"] * 1000, 1)
    return {"ttft_ms": ttft_ms,
            "prefill_ms": derived["prefill_ms"] if measured else None,
            "decode_tok_s": derived["decode_tok_s"] if measured else None,
            "prefill_tok_s": derived["prefill_tok_s"] if measured else None,
            "prompt_tokens": derived["prompt_tokens"] if counted else None,
            "out_tokens": derived["out_tokens"] if counted else None,
            "cached_tokens": derived["cached_tokens"] if usage else None,
            "total_ms": round(total_ms, 1),
            "finish_reason": finish_reason,
            "device": device,
            "model": model}


def io_capabilities(wants, gives):
    """The device's input and output words for a model, as lists.

    The device answers with one word a side, not with lists: "Text" and "Text"
    for a chat model, "Text" and "Vector" for the embedding model. They are
    wrapped rather than reshaped, so nothing is invented and a caller has one
    shape to read. A record carrying neither gets null, because an empty list
    would say this model takes nothing, which is a different claim.
    """
    def listed(value):
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    ins, outs = listed(wants), listed(gives)
    if not ins and not outs:
        return None
    return {"input": ins, "output": outs}


def catalog_url(model_id):
    """A link out for the model card, or None.

    Neither the installed record nor the device's own catalogue carries a URL for
    a model: the only links on either are icon paths on the device itself. Model
    ids here are Hugging Face repository paths, though, so one owner and one name
    is a page that exists. This is a derivation from the shape of the id and not
    a field anybody sent, which is why anything else gets null rather than a
    guess at a URL.
    """
    parts = str(model_id or "").split("/")
    if len(parts) != 2 or not all(part.strip() and " " not in part for part in parts):
        return None
    return "https://huggingface.co/%s" % model_id


def split_done(blob):
    """(everything before the [DONE] line, the [DONE] line and what follows).

    The sentinel counts only as a whole line of its own. Ask a model about
    streaming and it writes "data: [DONE]" into its answer, where it reaches
    this function inside a frame's JSON string: a match anywhere in the bytes
    cut that frame in half, so the browser got something it could not parse, the
    answer stopped mid-sentence with nothing saying why, and the gateway's last
    chunk, the one carrying timings and usage, was never read. The release whose
    whole point is the numbers then reported zeroes. A JSON string cannot hold a
    raw newline, so a newline in this blob is always a frame boundary and
    anchoring to one is enough.
    """
    index = 0
    while True:
        index = blob.find(DONE_LINE, index)
        if index < 0:
            return blob, None
        starts_line = index == 0 or blob[index - 1:index] == b"\n"
        ends_line = blob[index + len(DONE_LINE):index + len(DONE_LINE) + 1] in (
            b"", b"\n", b"\r")
        if starts_line and ends_line:
            return blob[:index], blob[index:]
        index += len(DONE_LINE)


class ChatWatch:
    """Reads the frames going past on their way to the browser.

    The stream is relayed to the browser exactly as the device sent it, so the
    only way to know what was in it is to read the bytes on the way through.
    Three things are wanted: when the first token arrived by this machine's
    clock, why generation stopped, and the timings and usage blocks the gateway
    puts in the last chunk of a stream that asked for them.
    """

    def __init__(self, started):
        self.started = started
        self.ttft_ms = None
        self.finish_reason = None
        self.timings = {}
        self.usage = {}

    def read(self, blob):
        for line in blob.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                frame = json.loads(data)
            except ValueError:
                continue
            if not isinstance(frame, dict):
                continue
            if isinstance(frame.get("timings"), dict):
                self.timings = frame["timings"]
            if isinstance(frame.get("usage"), dict):
                self.usage = frame["usage"]
            # The last chunk of a stream carries the numbers and an empty
            # choices list, so nothing here may assume there is a choices[0].
            choices = frame.get("choices") or []
            first = choices[0] if choices and isinstance(choices[0], dict) else {}
            if first.get("finish_reason"):
                self.finish_reason = first["finish_reason"]
            delta = first.get("delta") or {}
            if self.ttft_ms is None and (delta.get("content")
                                         or delta.get("reasoning_content")):
                # A reasoning token counts as the first token. It is a predicted
                # token like any other and it is what the benchmark's ttft_s
                # measures; waiting for the answer instead would report nothing
                # at all for a thinking model that spent its whole budget
                # thinking, which is the common case with a modest cap.
                self.ttft_ms = round((time.time() - self.started) * 1000, 1)


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
            # Anything else with a type we serve is a file in web/. This used to
            # be a two entry list of app.css and app.js, so the day the page
            # grew a third file that file 404'd and nobody could see why.
            if not path.startswith("/api/") \
                    and CONTENT_TYPES.get(os.path.splitext(path)[1]):
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
        if path == "/api/model_card":
            model_id = self.one("model") or ""
            if not model_id:
                return self.send_json(400, {"error": {"message": "model is required"}})
            try:
                card = self.model_card(model_id)
            except device_mod.DeviceError as exc:
                return self.send_json(502, {"error": {"message": str(exc)}})
            if card is None:
                return self.send_json(404, {"error": {
                    "message": "no device in this fleet has a model called %r"
                               % model_id}})
            return self.send_json(200, card)
        if path == "/api/instances":
            return self.send_json(200, self.instances())
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
            if path == "/api/chat":
                return self.page_chat(payload)
            if path == "/api/instances/load":
                return self.instance_load(payload)
            if path == "/api/instances/unload":
                return self.instance_unload(payload)
            if path == "/api/devices":
                return self.add_device(payload)
            if path == "/api/devices/unlock":
                return self.unlock_device(payload)
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

    # ------------------------------------------------------------------ chat
    @staticmethod
    def chat_request(body):
        """The completion to send on, or a sentence saying what is wrong.

        The Chat page could call /v1/chat/completions directly, and used to, but
        then every number on the screen would be a guess made in the browser by
        a clock that never saw the device. This is the same completion with the
        controls the page owns folded in.
        """
        model_id = (body.get("model") or "").strip()
        if not model_id:
            return "field 'model' is required"
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return "field 'messages' must be a non-empty array"
        try:
            max_tokens = int(body.get("max_tokens") or DEFAULT_MAX_TOKENS)
        except (TypeError, ValueError):
            return "field 'max_tokens' must be a number"
        out = list(messages)
        system = (body.get("system") or "").strip()
        first = out[0] if isinstance(out[0], dict) else {}
        if system and first.get("role") != "system":
            out.insert(0, {"role": "system", "content": system})
        request = {
            "model": model_id, "messages": out, "max_tokens": max_tokens,
            # chat_template_kwargs is the only knob that turns reasoning on and
            # off. The gateway's own OpenAPI document declares a top level
            # enable_thinking as well, with thinking_enabled, reasoning_effort
            # and thinking_budget_tokens beside it, and the runtime ignores all
            # of them: measured, a request sending enable_thinking false at the
            # top level still got a chain of thought back.
            "chat_template_kwargs": {"enable_thinking": bool(body.get("thinking"))}}
        temperature = body.get("temperature")
        if isinstance(temperature, (int, float)) and not isinstance(temperature, bool):
            request["temperature"] = float(temperature)
        if body.get("stream"):
            request["stream"] = True
            # Without this a stream carries no timings and no usage at all, and
            # the stats bar would have nothing but the browser's own clock.
            request["stream_options"] = {"include_usage": True}
        return request

    def page_chat(self, body):
        """POST /api/chat: a completion with the numbers attached."""
        request = self.chat_request(body)
        if isinstance(request, str):
            return self.send_json(400, {"error": {"message": request}})
        model_id = request["model"]
        started = time.time()
        if not request.get("stream"):
            status, payload = gateway.chat(self.fleet, request)
            self.fleet.invalidate()
            if status != 200 or not isinstance(payload, dict):
                return self.send_json(status, payload)
            stamp = payload.get("ainode_pocket") or {}
            choices = payload.get("choices") or []
            choice = choices[0] if choices and isinstance(choices[0], dict) else {}
            payload["stats"] = chat_stats(
                payload.get("timings"), payload.get("usage"),
                total_ms=(time.time() - started) * 1000, ttft_ms=None,
                finish_reason=choice.get("finish_reason"),
                device={"id": stamp.get("device"), "name": stamp.get("device_name")},
                model=model_id)
            return self.send_json(200, payload)

        served = {}
        status, refusal, stream = gateway.chat_stream(
            self.fleet, request,
            on_device=lambda dev: served.update({"id": dev.id, "name": dev.name}))
        if stream is None:
            return self.send_json(status, refusal)
        watch = ChatWatch(started)
        self.begin_sse()
        try:
            finished = False
            for blob in stream:
                if finished:
                    # Everything after the first [DONE] is unreachable: the
                    # browser has stopped reading. The generator is drained
                    # anyway rather than abandoned, because it holds the device
                    # lock until it ends.
                    continue
                head, tail = split_done(blob)
                if head:
                    watch.read(head)
                    self.write_chunk(head)
                if tail is None:
                    continue
                self.write_chunk(self.stats_event(watch, served, model_id, started))
                self.write_chunk(DONE_LINE + b"\n\n")
                finished = True
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.fleet.invalidate()
        return None

    def stats_event(self, watch, served, model_id, started):
        payload = chat_stats(watch.timings, watch.usage,
                             total_ms=(time.time() - started) * 1000,
                             ttft_ms=watch.ttft_ms,
                             finish_reason=watch.finish_reason,
                             device={"id": served.get("id"),
                                     "name": served.get("name")},
                             model=model_id)
        return ("event: stats\ndata: %s\n\n" % json.dumps(payload)).encode()

    # ------------------------------------------------------------- instances
    def model_card(self, model_id):
        """What the card beside the conversation shows, or None if nobody has it.

        Read from each device's own installed record rather than from the fleet
        index, because input, output and the description live on that record and
        the index throws them away.
        """
        entry, loaded_on = None, []
        for device_id in list(self.fleet.devices):
            try:
                dev = self.fleet.get(device_id)
                rows = dev.models()
            except (NoDevice, device_mod.DeviceError):
                continue
            match = None
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if (row.get("model_id") or row.get("id") or row.get("name")) == model_id:
                    match = row
                    break
            if match is None:
                continue
            if entry is None:
                entry = match
            for inst in (self.fleet.telemetry(device_id).get("instances") or []):
                if inst.get("model_id") != model_id:
                    continue
                loaded_on.append({"device_id": device_id, "device_name": dev.name,
                                  "status": inst.get("status") or "running"})
        if entry is None:
            return None
        return {"model": model_id,
                "name": entry.get("display_name") or entry.get("name") or model_id,
                "type": entry.get("type") or None,
                "params": entry.get("params") or None,
                "size_bytes": entry.get("size") or entry.get("total_size") or None,
                "npu_usage": entry.get("npu_usage"),
                "capabilities": io_capabilities(entry.get("input"), entry.get("output")),
                "loaded_on": loaded_on,
                "catalog_url": catalog_url(model_id),
                "can_chat": device_mod.can_chat(entry.get("type"),
                                                device_mod.capability_list(entry)),
                "desc": entry.get("desc") or None}

    def instances(self):
        """Every loaded model on every device, read live.

        Not through the telemetry cache: the load panel polls this to watch a
        model go from loading to running, and a four second cache would hide the
        move it exists to show. `reachable` is the same fact /api/state calls
        `online`, answered by whether these two calls came back just now.
        """
        rows, devices = [], []
        for device_id in list(self.fleet.devices):
            try:
                dev = self.fleet.get(device_id)
            except NoDevice:
                continue
            record = {"device_id": device_id, "device_name": dev.name,
                      "npu_used": None, "npu_total": None, "reachable": False}
            try:
                units = dev.npu_units()
                running = dev.running()
            except device_mod.DeviceError:
                devices.append(record)
                continue
            record["npu_used"] = units.get("npu_used") or 0
            record["npu_total"] = units.get("npu_total") or 0
            record["reachable"] = True
            devices.append(record)
            budget = {row.get("model_id"): row for row in (units.get("models") or [])
                      if isinstance(row, dict)}
            for inst in ((running.get("instances") or {}).get("running") or []):
                if not isinstance(inst, dict):
                    continue
                model_id = inst.get("model_id")
                spare = budget.get(model_id) or {}
                rows.append({"device_id": device_id, "device_name": dev.name,
                             "model": model_id,
                             "npu_usage": inst.get("npu_usage") or spare.get("npu_usage"),
                             "status": inst.get("status") or spare.get("status")
                                       or "running",
                             "instance_id": inst.get("instance_id")
                                            or spare.get("instance_id")})
        return {"instances": rows, "devices": devices}

    def refuse_load(self, dev, model_id, chat_required):
        """Why this model cannot be loaded on this device, or None.

        The NPU budget is checked here rather than left to the device because the
        device does not refuse: a start that does not fit is accepted with the
        same 200 as any other, sits in npu/status as loading, and then vanishes
        with no error anywhere. This is the only place anybody gets told. It is
        not a guarantee either, because the subtraction is not the whole
        constraint on that hardware, which is why the panel polls afterwards.
        """
        row = None
        for candidate in self.fleet.models(dev.id):
            if candidate["model_id"] == model_id:
                row = candidate
                break
        if row is None:
            return ("%s is not installed on %s. Download it on the Models page "
                    "first; nothing is fetched from the catalogue on demand."
                    % (model_id, dev.name))
        if chat_required and not row["chat"]:
            phrase = device_mod.type_phrase(row["type"])
            what = ("is %s and cannot chat" % phrase) if phrase \
                else "is not a chat model"
            return ("%s %s, so loading it here would not give you anything to "
                    "talk to." % (model_id, what))
        if row["loaded"]:
            # Already there. The device answers "already running" and the rail
            # shows it, which is a better answer than an error.
            return None
        units = dev.npu_units()
        total = units.get("npu_total") or 0
        used = units.get("npu_used") or 0
        cost = row.get("npu_usage") or 0
        if total and used + cost > total:
            return ("%s needs %d NPU units and only %d of %d are free on %s. The "
                    "device accepts a load that does not fit and then rolls it "
                    "back without saying so, so it is refused here instead."
                    % (model_id, cost, total - used, total, dev.name))
        return None

    def instance_target(self, payload):
        """(device, model_id) for a load or unload, or a (status, error) pair."""
        device_id = (payload.get("device_id") or payload.get("device") or "").strip()
        model_id = (payload.get("model") or "").strip()
        if not device_id or not model_id:
            return None, (400, "device_id and model are required")
        try:
            return self.fleet.get(device_id), model_id
        except NoDevice as exc:
            return None, (404, str(exc))

    def instance_load(self, payload):
        dev, rest = self.instance_target(payload)
        if dev is None:
            return self.send_json(rest[0], {"error": {"message": rest[1]}})
        model_id = rest
        try:
            refusal = self.refuse_load(dev, model_id, chat_required=True)
            if refusal:
                return self.send_json(400, {"error": {"message": refusal}})
            dev.start(model_id)
        except device_mod.DeviceError as exc:
            return self.send_json(502, {"error": {"message": str(exc),
                                                  "code": exc.code}})
        self.fleet.invalidate(dev.id)
        # 202 because the device has only accepted the job. Whether the model
        # comes up is answered by polling /api/instances, and by a chat that
        # works, not by this reply.
        return self.send_json(202, {"ok": True})

    def instance_unload(self, payload):
        dev, rest = self.instance_target(payload)
        if dev is None:
            return self.send_json(rest[0], {"error": {"message": rest[1]}})
        model_id = rest
        try:
            if model_id not in set(dev.running().get("running") or []):
                return self.send_json(400, {"error": {
                    "message": "%s is not loaded on %s." % (model_id, dev.name)}})
            dev.stop(model_id)
        except device_mod.DeviceError as exc:
            return self.send_json(502, {"error": {"message": str(exc),
                                                  "code": exc.code}})
        self.fleet.invalidate(dev.id)
        return self.send_json(202, {"ok": True})

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
        password = (payload.get("password") or "").strip()
        if not key and password and address:
            # A box that never had TiinyOS run against it has no local-storage
            # key to find, locked or not. Its own account API hands one over
            # directly -- see ~/code/tiiny/tools/README-unlock.md.
            resolved_id = device_mod.identity(device_mod.probe(address), address)
            key = device_mod.account_auth_key(address, resolved_id, password)
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

    def unlock_device(self, payload):
        """Get a fresh key for a registered device, e.g. after it relocked.

        Same account-API call add_device tries on first registration, but
        for a box that's already in the fleet and just needs a new key --
        the "Unlock" action on a red card, not a re-add.
        """
        device_id = (payload.get("id") or "").strip()
        password = (payload.get("password") or "").strip()
        if not device_id or not password:
            return self.send_json(400, {"error": {"message": "id and password are required"}})
        try:
            telemetry = self.fleet.unlock(device_id, password)
        except NoDevice as exc:
            return self.send_json(400, {"error": {"message": str(exc)}})
        return self.send_json(200, {"telemetry": telemetry})

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
                # The same guard the Chat page's load panel uses, minus the
                # chat requirement: this page loads speech and embedding models
                # on purpose. One guard rather than two, so the two doors onto
                # dev.start cannot drift apart.
                refusal = self.refuse_load(dev, model_id, chat_required=False)
                if refusal:
                    return self.send_json(400, {"error": {"message": refusal}})
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
