"""One OpenAI-compatible endpoint in front of a fleet of Tiinys.

    GET  /v1/models            the union of models across every device
    POST /v1/chat/completions   routed by model id, streaming or not

The contract is deliberately small and honest:

  * A model that cannot chat is refused here, with a 400, before any device is
    touched. A Tiiny holds speech, embedding and image models alongside the
    chat ones and will happily be asked to chat with a text-to-speech model;
    what comes back is a device error about an unsupported model, which reads
    like a broken app rather than a wrong choice.
  * A request is routed to a device that already has the model loaded. Models do
    not auto-load on this hardware and Pocket does not load one behind your back
    to serve a chat, because a 35B takes tens of seconds to come up and a silent
    stall is worse than a clear refusal.
  * Requests to one device are serialised through that device's lock, so
    concurrent callers queue instead of colliding with error 150004.
  * When nothing can serve the request the reply is a 503 that says why.
"""
from __future__ import annotations

import json
import random
import time

from . import device as device_mod
from .fleet import NoDevice

# A 150004 while we hold the lock means somebody outside it has the device: a
# neighbour application not going through OneLane, or the device doing its own
# work. Backing off briefly is cheap and usually wins.
BUSY_RETRIES = 4
BUSY_BACKOFF = 1.5


def error(status, message, kind="unavailable", extra=None):
    payload = {"error": {"message": message, "type": kind, "code": status}}
    if extra:
        payload["error"].update(extra)
    return status, payload


def models_payload(fleet, loaded_only=False):
    """The union across devices, in OpenAI /v1/models shape.

    Every model installed anywhere in the fleet is listed, each annotated with
    the devices that hold it and whether any of them has it loaded. A client can
    filter with ?loaded=1 to see only what will answer right now.
    """
    index = fleet.index()
    data = []
    for model_id in sorted(index):
        slot = index[model_id]
        ready = bool(slot["loaded_on"])
        if loaded_only and not ready:
            continue
        names = [fleet.devices[d].name for d in slot["devices"] if d in fleet.devices]
        data.append({
            "id": model_id,
            "object": "model",
            "created": 0,
            "owned_by": names[0] if len(names) == 1 else "%d devices" % len(names),
            # Namespaced so a strict OpenAI client ignores it and the UI can use it.
            "ainode_pocket": {
                "ready": ready,
                # Every model stays in the list, because that is what the
                # OpenAI contract says /v1/models is. Whether this endpoint can
                # chat with it is a separate question, answered here.
                "chat": slot["chat"],
                "type": slot["type"],
                "capabilities": slot["capabilities"],
                "params": slot["params"],
                "size": slot["size"],
                "devices": slot["devices"],
                "loaded_on": slot["loaded_on"]}})
    return {"object": "list", "data": data}


def _validate(body):
    if not isinstance(body, dict):
        return error(400, "request body must be a JSON object", "invalid_request_error")
    if not body.get("model"):
        return error(400, "field 'model' is required", "invalid_request_error")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return error(400, "field 'messages' must be a non-empty array",
                     "invalid_request_error")
    return None


def refuse_non_chat(fleet, model_id):
    """A 400 for a model that cannot chat, or None.

    Returned before routing, so nothing is asked of a device. A model Pocket
    has never heard of is not refused here: that is the router's 503, which can
    say which devices hold what.

    Public because the benchmark needs the same answer in the same words. Every
    section of that suite is a chat completion, so a model that cannot chat
    cannot be benchmarked either, and two ways of saying so would drift.
    """
    slot = fleet.index().get(model_id)
    if slot is None or slot["chat"]:
        return None
    phrase = device_mod.type_phrase(slot["type"])
    head = ("%s is %s and cannot chat." % (model_id, phrase) if phrase
            else "%s is not a chat model." % model_id)
    loaded = fleet.chat_models()
    if loaded:
        tail = "Loaded chat models: %s." % ", ".join(loaded)
    else:
        tail = ("No chat model is loaded right now. Load one on the Models "
                "page first.")
    return error(400, "%s %s" % (head, tail), "invalid_request_error")


def _pick(fleet, model_id):
    try:
        return fleet.route(model_id)
    except NoDevice as exc:
        raise exc


def chat(fleet, body, timeout=240):
    """A non-streaming completion. Returns (status, payload)."""
    bad = _validate(body)
    if bad:
        return bad
    model_id = body["model"]
    refusal = refuse_non_chat(fleet, model_id)
    if refusal:
        return refusal
    try:
        dev, lock = fleet.route(model_id)
    except NoDevice as exc:
        return error(503, str(exc), "model_not_available")

    with lock.turn(why="chat %s" % model_id):
        fleet.invalidate(dev.id)
        for attempt in range(BUSY_RETRIES):
            try:
                payload = dev.chat(body, timeout=timeout)
            except device_mod.DeviceBusy:
                if attempt == BUSY_RETRIES - 1:
                    return error(503,
                                 "%s stayed busy through %d attempts. Something "
                                 "outside this app's lock is using the device; "
                                 "error 150004 means busy, not a bad request."
                                 % (dev.name, BUSY_RETRIES), "device_busy",
                                 {"device": dev.id})
                time.sleep(BUSY_BACKOFF * (attempt + 1) + random.random() * 0.3)
                continue
            except device_mod.DeviceTimeout as exc:
                return error(504,
                             "%s. The device gateway closes a single request at "
                             "about 220 seconds; ask for less in one call or use "
                             "stream=true so partial output survives." % exc,
                             "gateway_timeout", {"device": dev.id})
            except device_mod.DeviceError as exc:
                return error(502, "%s: %s" % (dev.name, exc), "device_error",
                             {"device": dev.id})
            if isinstance(payload, dict):
                payload.setdefault("model", model_id)
                payload["ainode_pocket"] = {"device": dev.id, "device_name": dev.name}
            return 200, payload
    return error(503, "no device answered", "model_not_available")


def chat_stream(fleet, body, timeout=600, on_device=None):
    """A streaming completion.

    Returns (status, payload, None) for a failure that happens before any bytes
    go out, or (200, None, generator) where the generator yields SSE bytes. The
    device lock is held for the whole stream, which is the only way a second
    caller genuinely queues rather than colliding.

    `on_device` is called with the device this request was routed to, before any
    byte leaves. A non-streamed reply says which box answered in its
    ainode_pocket block; a stream has nowhere to put that, and the chat page has
    to name the device beside the numbers, so the caller is told directly rather
    than routing a second time and possibly getting a different answer.
    """
    bad = _validate(body)
    if bad:
        return bad[0], bad[1], None
    model_id = body["model"]
    refusal = refuse_non_chat(fleet, model_id)
    if refusal:
        return refusal[0], refusal[1], None
    try:
        dev, lock = fleet.route(model_id)
    except NoDevice as exc:
        status, payload = error(503, str(exc), "model_not_available")
        return status, payload, None
    if on_device is not None:
        on_device(dev)

    def produce():
        fd = lock.acquire(why="chat stream %s" % model_id)
        try:
            fleet.invalidate(dev.id)
            saw_content = False
            for attempt in range(BUSY_RETRIES):
                busy = False
                try:
                    for line in dev.chat_stream(body, timeout=timeout):
                        stripped = line.strip()
                        if not stripped:
                            continue
                        if not saw_content and stripped.startswith("{"):
                            # A busy refusal can arrive as a bare JSON body
                            # instead of an SSE frame.
                            try:
                                probe = json.loads(stripped)
                            except ValueError:
                                probe = None
                            if isinstance(probe, dict) and probe.get("code") == \
                                    device_mod.BUSY_CODE:
                                busy = True
                                break
                        saw_content = True
                        if stripped == "data: [DONE]":
                            # The device ends its stream with one and this
                            # generator appends its own below, so relaying the
                            # device's put two in every stream. A client stops
                            # reading at the first, so the second was never seen
                            # and anything appended after it never would be.
                            continue
                        yield (line if line.endswith("\n") else line + "\n").encode()
                except device_mod.DeviceBusy:
                    busy = True
                except device_mod.DeviceTimeout as exc:
                    yield _sse_error(
                        "%s. The gateway closes a request at about 220 seconds; "
                        "the tokens already streamed above are real." % exc)
                    return
                except device_mod.DeviceError as exc:
                    yield _sse_error("%s: %s" % (dev.name, exc))
                    return
                if not busy:
                    yield b"data: [DONE]\n\n"
                    return
                if saw_content or attempt == BUSY_RETRIES - 1:
                    yield _sse_error(
                        "%s reported error 150004 (busy) through %d attempts."
                        % (dev.name, attempt + 1))
                    return
                time.sleep(BUSY_BACKOFF * (attempt + 1))
        finally:
            lock.release(fd)

    return 200, None, produce()


def _sse_error(message):
    blob = json.dumps({"error": {"message": message, "type": "device_error"}})
    return ("data: %s\n\ndata: [DONE]\n\n" % blob).encode()
