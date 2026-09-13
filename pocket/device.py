"""Tiiny device client. Standard library only, Python 3.9+.

One Tiiny Pocket Lab runs seven HTTP services on different ports. Pocket only
needs three of them:

    8800   AI gateway: OpenAI-compatible inference plus the model lifecycle
    80     device management: /api/v1/sys/* telemetry
    39218  discovery: /device.json, unauthenticated

Every service takes the same static bearer key. Model ids contain a slash and
must be URL-encoded in paths, which is the most common cause of spurious 404s.

Two device behaviours shape this whole file and are documented in
/Users/sem/code/tiiny/CAPABILITIES.md:

    The NPU runs one inference at a time and does not batch. A second request
    arriving mid-inference comes back as HTTP 200 with an in-band body of
    {"code": 150004, "message": "The operation failed to complete."}.

    The gateway closes any single request at about 220 seconds with HTTP 504,
    measured at 222.3s after 580 tokens. Streaming is the only way partial
    output survives that ceiling.
"""
from __future__ import annotations

import glob
import json
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

GATEWAY_PORT = 8800
MGMT_PORT = 80
DISCOVERY_PORT = 39218

BUSY_CODE = 150004
KEY_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class DeviceError(Exception):
    """A device call failed. `code` is the device's own error code when it sent one."""

    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


class DeviceBusy(DeviceError):
    """Device error 150004: the NPU is already running an inference."""


class DeviceTimeout(DeviceError):
    """The gateway closed the request. At ~220s this is the documented ceiling."""


def enc(model_id):
    """URL-encode a model id for use in a path segment."""
    return urllib.parse.quote(str(model_id), safe="")


def _read_error(payload):
    """Return (code, message) if this JSON body is a device error, else None.

    Three shapes are in play, all recorded:
      {"code": 150004, "message": "The operation failed to complete."}   in-band, HTTP 200
      {"code": 400, "msg": "...", "detail": "..."}                       ErrorResponse
      {"error": {"code": 404, "message": "\"x\" is not loaded.", ...}}   not-loaded
    A BaseResponse with code 0 is a success and must not be read as a failure.
    """
    if not isinstance(payload, dict):
        return None
    err = payload.get("error")
    if isinstance(err, dict) and err.get("code") is not None:
        return err.get("code"), str(err.get("message") or err.get("msg") or "device error")
    code = payload.get("code")
    if isinstance(code, int) and code != 0 and "choices" not in payload:
        return code, str(payload.get("message") or payload.get("msg") or "device error")
    return None


class Device:
    """One Tiiny, addressed by its three base URLs.

    Real devices derive all three from one address. The fake device in
    pocket/fake.py serves all three surfaces on a single port, which is why the
    base URLs are explicit rather than a host plus hardcoded ports.
    """

    def __init__(self, device_id, name, address, key="",
                 gateway=None, mgmt=None, discovery=None, timeout=30):
        self.id = device_id
        self.name = name
        self.address = address
        self.key = key or ""
        self.gateway = (gateway or "http://%s:%d" % (address, GATEWAY_PORT)).rstrip("/")
        self.mgmt = (mgmt or "http://%s" % address).rstrip("/")
        self.discovery = (discovery or "http://%s:%d" % (address, DISCOVERY_PORT)).rstrip("/")
        self.timeout = timeout

    # ------------------------------------------------------------------ core
    def _open(self, base, method, path, body=None, timeout=None, stream=False):
        url = base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if self.key:
            req.add_header("Authorization", "Bearer " + self.key)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if stream:
            req.add_header("Accept", "text/event-stream")
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            if exc.code in (502, 503, 504):
                raise DeviceTimeout(
                    "HTTP %d from %s. At about 220 seconds this is the gateway's "
                    "documented per-request ceiling; use streaming for long output."
                    % (exc.code, url), status=exc.code) from None
            code = None
            try:
                code = (_read_error(json.loads(detail)) or (None, None))[0]
            except Exception:
                pass
            raise DeviceError("HTTP %d from %s: %s" % (exc.code, path, detail),
                              status=exc.code, code=code) from None
        except (socket.timeout, TimeoutError):
            raise DeviceTimeout("timed out after %ss calling %s"
                                % (timeout or self.timeout, path)) from None
        except OSError as exc:
            raise DeviceError("%s unreachable: %s" % (url, exc)) from None

    def call(self, base, method, path, body=None, timeout=None):
        """One JSON call. Raises DeviceBusy on the in-band 150004."""
        resp = self._open(base, method, path, body, timeout)
        try:
            raw = resp.read().decode("utf-8", "replace")
        finally:
            resp.close()
        if not raw.strip():
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            raise DeviceError("non-JSON reply from %s: %s" % (path, raw[:120])) from None
        found = _read_error(payload)
        if found:
            code, message = found
            if code == BUSY_CODE:
                raise DeviceBusy(message, code=code)
            raise DeviceError("%s (device code %s)" % (message, code), code=code)
        return payload

    def gw(self, method, path, body=None, timeout=None):
        return self.call(self.gateway, method, path, body, timeout)

    def sys(self, method, path, body=None, timeout=None):
        return self.call(self.mgmt, method, path, body, timeout)

    # ------------------------------------------------------------ telemetry
    def device_json(self, timeout=5):
        """Unauthenticated identity from the discovery service on 39218."""
        return self.call(self.discovery, "GET", "/device.json", timeout=timeout)

    def device_info(self):
        return self.sys("GET", "/api/v1/sys/device_info") or {}

    def sys_status(self):
        """Per-core CPU, memory, disk, and the npus array. The gpu block is a
        placeholder in this firmware, so it is not read."""
        return self.sys("GET", "/api/v1/sys/status") or {}

    # --------------------------------------------------------------- models
    def models(self):
        """Installed models. OpenAIModelList: {"object": "list", "data": [...]}."""
        payload = self.gw("GET", "/api/v1/models/")
        if isinstance(payload, list):
            return payload
        return (payload or {}).get("data") or []

    def running(self):
        """{"running": [model_id, ...], "instances": {"running": [...]}}"""
        return self.gw("GET", "/api/v1/models/running") or {}

    def npu_units(self):
        """{"npu_total": 100, "npu_used": 50, "npu_available": 50, "models": [...]}

        NPU accounting is in percent units of memory residency, not bytes and
        not a compute reservation. A 35B model costs 50 of 100.
        """
        return self.gw("GET", "/api/v1/models/npu/status") or {}

    def npu_devices(self):
        """NpuStatusResponse from /api/v1/npu/status: hm_smi_available, devices,
        cpu, memory, occupants, npu. devices[] carries temp_c and power_w, which
        is the only place the REST surface declares a temperature at all."""
        return self.gw("GET", "/api/v1/npu/status") or {}

    def catalog(self, timeout=60):
        payload = self.gw("GET", "/api/v1/models/online_models", timeout=timeout)
        if isinstance(payload, list):
            return payload
        return (payload or {}).get("data") or []

    def model_storage(self, timeout=60):
        return self.gw("GET", "/api/v1/models/storage", timeout=timeout) or {}

    def start(self, model_id, timeout=300):
        """Load into the NPU. Asynchronous: replies {"message": "start loading
        <id>", "progress": 0} and the model appears in running() later."""
        return self.gw("POST", "/api/v1/models/%s/start" % enc(model_id), timeout=timeout)

    def stop(self, model_id, timeout=120):
        """Unload. Replies {"removed_container_ids": [...]}."""
        return self.gw("POST", "/api/v1/models/%s/stop" % enc(model_id), timeout=timeout)

    def delete(self, model_id, timeout=120):
        return self.gw("DELETE", "/api/v1/models/%s" % enc(model_id), timeout=timeout)

    def download(self, model_id, timeout=60):
        return self.gw("POST", "/api/v1/models/%s/download" % enc(model_id), timeout=timeout)

    def progress(self, model_id, timeout=30):
        return self.gw("GET", "/api/v1/models/%s/get_progress" % enc(model_id),
                       timeout=timeout) or {}

    def download_stream(self, model_id, timeout=600):
        """SSE sibling of the download call. Yields raw decoded SSE lines."""
        resp = self._open(self.gateway, "POST",
                          "/api/v1/models/%s/download/stream" % enc(model_id),
                          timeout=timeout, stream=True)
        try:
            for raw in resp:
                yield raw.decode("utf-8", "replace").rstrip("\n")
        finally:
            resp.close()

    # ------------------------------------------------------------ inference
    def chat(self, body, timeout=240):
        return self.gw("POST", "/v1/chat/completions", body, timeout=timeout)

    def chat_stream(self, body, timeout=600):
        """Yield raw SSE lines from a streaming completion.

        The in-band busy error can arrive as the first line of the stream
        instead of a normal HTTP error, so the caller checks for it.
        """
        payload = dict(body)
        payload["stream"] = True
        resp = self._open(self.gateway, "POST", "/v1/chat/completions",
                          payload, timeout=timeout, stream=True)
        try:
            for raw in resp:
                yield raw.decode("utf-8", "replace")
        finally:
            resp.close()


# ------------------------------------------------------------------ discovery
def probe(address, timeout=3):
    """Read /device.json from one address. Unauthenticated, so this works
    before a key is known. Returns the parsed payload or None."""
    url = "http://%s:%d/device.json" % (address, DISCOVERY_PORT)
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def scan(subnet, timeout=1.0, workers=64):
    """Probe every host in a /24 for a discovery responder. Opt-in only.

    `subnet` is "192.168.100" or "192.168.100.0/24". Returns a list of
    (address, device.json) pairs.
    """
    import concurrent.futures

    base = subnet.split("/")[0].strip()
    parts = [p for p in base.split(".") if p != ""]
    if len(parts) == 4:
        parts = parts[:3]
    if len(parts) != 3:
        raise ValueError("subnet must look like 192.168.100 or 192.168.100.0/24")
    prefix = ".".join(parts)
    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(probe, "%s.%d" % (prefix, n), timeout): "%s.%d" % (prefix, n)
                for n in range(1, 255)}
        for job in concurrent.futures.as_completed(jobs):
            payload = job.result()
            if payload:
                found.append((jobs[job], payload))
    found.sort(key=lambda pair: [int(x) for x in pair[0].split(".")])
    return found


def identity(payload, address):
    """A stable id for a device from its device.json, falling back to address."""
    if isinstance(payload, dict):
        for field in ("sn", "serial", "serial_number", "device_id", "deviceId", "id"):
            value = payload.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return address


# ------------------------------------------------------------------- the key
def tiinyapps_settings(path=None):
    """Read ~/.tiinyapps/device.json, the file the farm CLI writes.

    Shape is {"base": "http://<host>:8800", "key": "<uuid>"}, mode 0600.
    """
    target = path or os.path.join(os.path.expanduser("~"), ".tiinyapps", "device.json")
    try:
        with open(target, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    base = payload.get("base")
    key = payload.get("key")
    if isinstance(base, str) and isinstance(key, str) and base and key:
        return {"base": base, "key": key}
    return None


def tiinyos_keys():
    """Candidate keys out of the TiinyOS desktop app's local storage.

    The device key is a UUID and the app keeps it in Electron leveldb. Same
    lookup tiiny-bench uses. macOS only; returns [] anywhere else.
    """
    pattern = os.path.join(os.path.expanduser("~"), "Library", "Application Support",
                           "TiinyOS", "Local Storage", "leveldb", "*.ldb")
    seen = []
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path, "rb") as handle:
                blob = handle.read()
        except OSError:
            continue
        for match in KEY_RE.findall(blob.decode("latin-1")):
            if match not in seen:
                seen.append(match)
    return seen


def find_key(verify=None):
    """Resolve a device key without ever storing one in this repo.

    Order: TIINY_KEY from the environment (the farm sets it), then
    ~/.tiinyapps/device.json, then the TiinyOS local storage lookup. When
    `verify` is given it is called with each candidate and the first one it
    accepts wins, which is how the local-storage scan picks the live key out of
    several UUIDs.
    """
    candidates = []
    env = os.environ.get("TIINY_KEY", "").strip()
    if env:
        candidates.append(env)
    settings = tiinyapps_settings()
    if settings and settings["key"] not in candidates:
        candidates.append(settings["key"])
    for found in tiinyos_keys():
        if found not in candidates:
            candidates.append(found)
    if verify is None:
        return candidates[0] if candidates else ""
    for candidate in candidates:
        try:
            if verify(candidate):
                return candidate
        except Exception:
            continue
    return ""


def base_url_parts(base):
    """Split a base URL like http://192.168.100.70:8800 into (host, port)."""
    parsed = urllib.parse.urlsplit(base if "//" in base else "http://" + base)
    return parsed.hostname or "", parsed.port
