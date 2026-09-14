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

A third one shapes how every gateway call is addressed. On firmware where port
8800 is closed to the LAN, the same service is still reachable on port 80 by
virtual host, which is how the vendor CLI addresses it
(/Users/sem/code/tiiny-sdk-docs/reference/http.md, "By IP with an explicit Host
header"):

    curl http://<device> /api/v1/models/npu/status -H 'Host: p8800.api.tiiny'
    curl http://<device> /v1/models               -H 'Host: openai.api.tiiny'

Without a Host header port 80 serves the device management UI, so the header is
not optional. Pocket tries the direct port first and falls back to the virtual
host only when the direct port refuses the connection, then remembers which one
worked so every later call goes straight there. A 401 or a 5xx means the port is
open and answering, so neither triggers the fallback.
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

# Virtual hosts the gateway answers to on port 80. Two of them, split by path:
# the model lifecycle and NPU endpoints live behind p8800, the OpenAI-compatible
# surface behind openai. Both verified against a live device.
GATEWAY_VHOST = "p8800.api.tiiny"
OPENAI_VHOST = "openai.api.tiiny"

TRANSPORT_DIRECT = "direct"
TRANSPORT_VHOST = "vhost"
TRANSPORT_LABELS = {TRANSPORT_DIRECT: "direct on port %d" % GATEWAY_PORT,
                    TRANSPORT_VHOST: "host header on port %d" % MGMT_PORT}

# A device answers on more than one plane at once. USB is a point-to-point /30
# that never changes; the LAN address is DHCP and will move. So USB is tried
# first for traffic, and the LAN address is the fallback for when the cable is
# out. Verified against a live device.json, which reports both.
PLANE_USB = "usb"
PLANE_LAN = "lan"
PLANE_PREFERENCE = (PLANE_USB, PLANE_LAN)
# Interface names the device reports for each plane in device.json.
PLANE_BY_INTERFACE = {"usb0": PLANE_USB, "wlan0": PLANE_LAN, "eth0": PLANE_LAN}

# The USB gadget link. device.json reports network 172.17.7.176/30 with the
# device on .177 and the host on .178, and several boxes can be plugged in at
# once, each on its own interface and its own /30.
USB_NET_PREFIX = "172.17."

UDP_DISCOVERY_PORT = 39217
# device.json advertises both the port and this token. Verified live: sending
# the token to the port, unicast or broadcast, gets the whole device.json back
# in one datagram.
DISCOVERY_TOKEN = b"GADGET_DISCOVER_V1"


def bracket(address):
    """Wrap a bare IPv6 address for use in a URL. IPv4 and hostnames pass through."""
    text = str(address or "")
    if ":" in text and not text.startswith("["):
        return "[%s]" % text
    return text


class Plane:
    """One way to reach a device: an address, and the base URLs that go with it."""

    __slots__ = ("name", "address", "gateway", "mgmt", "discovery", "vhost_base",
                 "interface")

    def __init__(self, name, address, gateway=None, mgmt=None, discovery=None,
                 vhost_base=None, interface=None):
        self.name = name
        self.address = address
        self.interface = interface
        host = bracket(address)
        self.gateway = (gateway or "http://%s:%d" % (host, GATEWAY_PORT)).rstrip("/")
        self.mgmt = (mgmt or "http://%s" % host).rstrip("/")
        self.discovery = (discovery or "http://%s:%d" % (host, DISCOVERY_PORT)).rstrip("/")
        # The virtual hosts answer on the management plane, port 80.
        self.vhost_base = (vhost_base or self.mgmt).rstrip("/")

    def base(self, surface):
        return getattr(self, surface)

    def as_dict(self):
        return {"name": self.name, "address": self.address,
                "interface": self.interface, "gateway": self.gateway,
                "mgmt": self.mgmt, "discovery": self.discovery,
                "vhost_base": self.vhost_base}

    def __repr__(self):
        return "Plane(%s, %s)" % (self.name, self.address)

# Cap on the first attempt while the transport is still unknown. A closed port
# refuses immediately, but a filtered one just hangs, and waiting the full
# request timeout before trying the fallback would make a reachable device look
# dead for minutes.
PROBE_TIMEOUT = 5


class DeviceError(Exception):
    """A device call failed. `code` is the device's own error code when it sent one."""

    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


class DeviceBusy(DeviceError):
    """Device error 150004: the NPU is already running an inference."""


class DeviceTimeout(DeviceError):
    """The gateway closed the request, or it never answered.

    `status` is the HTTP status when the device sent one. At ~220s an HTTP 504
    is the documented per-request ceiling. A `status` of None means nothing
    answered at all, which is the only kind worth retrying on another transport.
    """


class DeviceUnreachable(DeviceError):
    """Nothing accepted the connection. Retryable on another transport."""


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

    def __init__(self, device_id, name, address=None, key="",
                 gateway=None, mgmt=None, discovery=None, timeout=30,
                 transport=None, vhost_base=None, planes=None, plane=None):
        self.id = device_id
        self.name = name
        self.key = key or ""
        self.timeout = timeout
        if planes:
            self.planes = [p if isinstance(p, Plane) else Plane(**p) for p in planes]
        else:
            self.planes = [Plane(PLANE_LAN, address, gateway, mgmt, discovery,
                                 vhost_base)]
        self.planes.sort(key=lambda p: (PLANE_PREFERENCE.index(p.name)
                                        if p.name in PLANE_PREFERENCE else 9))
        # Both start undetermined: the next call finds out and remembers.
        self.plane = plane if any(p.name == plane for p in self.planes) else None
        self.transport = transport if transport in (TRANSPORT_DIRECT, TRANSPORT_VHOST) \
            else None
        # The fleet sets this so a resolved route is written to the registry and
        # does not have to be rediscovered on every restart.
        self.on_route_change = None

    # ----------------------------------------------------------------- planes
    @property
    def active(self):
        for plane in self.planes:
            if plane.name == self.plane:
                return plane
        return self.planes[0]

    @property
    def address(self):
        return self.active.address

    @property
    def addresses(self):
        return {plane.name: plane.address for plane in self.planes}

    @property
    def gateway(self):
        return self.active.gateway

    @property
    def mgmt(self):
        return self.active.mgmt

    @property
    def discovery(self):
        return self.active.discovery

    @property
    def vhost_base(self):
        return self.active.vhost_base

    def ordered_planes(self):
        """Which address to try, in order.

        USB first when the cable is in this host, because that link is fixed and
        the LAN address is DHCP. When it is not, the USB plane goes last rather
        than being dropped: it costs a six second hang to try, and it is still
        the right answer if the LAN address has moved and the cable is back.
        The plane that last worked wins, as long as it is still viable.
        """
        viable = [p for p in self.planes
                  if p.name != PLANE_USB or usb_reachable(p.address)]
        rest = [p for p in self.planes if p not in viable]
        ordered = viable + rest
        remembered = [p for p in viable if p.name == self.plane]
        if remembered:
            return remembered + [p for p in ordered if p is not remembered[0]]
        return ordered

    # ------------------------------------------------------------- transport
    @staticmethod
    def vhost_for(path):
        """Which virtual host serves this gateway path.

        The OpenAI-compatible surface is behind openai.api.tiiny; the model
        lifecycle and NPU endpoints are behind p8800.api.tiiny.
        """
        return OPENAI_VHOST if path.startswith("/v1/") or path == "/v1" \
            else GATEWAY_VHOST

    def attempts(self, surface, path):
        """Where to send this call, in order.

        Two axes. The plane is which address to use, USB before LAN because USB
        is a fixed point-to-point link and the LAN address is DHCP. The
        transport applies to the gateway only: the direct port first, then the
        virtual host on port 80. Whatever worked last is tried first, so a
        settled device makes exactly one attempt per call.
        """
        routes = []
        for plane in self.ordered_planes():
            if surface != "gateway":
                routes.append((plane, plane.base(surface), None))
                continue
            direct = (plane, plane.gateway, None)
            vhost = (plane, plane.vhost_base, self.vhost_for(path))
            if self.transport == TRANSPORT_VHOST:
                routes.append(vhost)
            elif self.transport == TRANSPORT_DIRECT:
                routes.append(direct)
            elif plane.vhost_base == plane.gateway:
                routes.append(direct)  # nowhere else to go
            else:
                routes.extend([direct, vhost])
        return routes

    def _remember(self, plane, transport):
        changed = False
        if plane is not None and self.plane != plane.name:
            self.plane = plane.name
            changed = True
        if transport is not None and self.transport != transport:
            self.transport = transport
            changed = True
        if changed and self.on_route_change is not None:
            try:
                self.on_route_change(self.plane, self.transport)
            except Exception:
                pass

    @property
    def transport_label(self):
        if self.transport is None:
            return "not determined yet"
        return TRANSPORT_LABELS.get(self.transport, self.transport)

    @property
    def route_label(self):
        plane = self.active
        where = "%s %s" % (plane.name, plane.address)
        others = [p.address for p in self.planes
                  if p.name != plane.name and p.address != plane.address]
        if others:
            where += " (also on %s)" % ", ".join(others)
        return "%s, gateway %s" % (where, self.transport_label)

    # ------------------------------------------------------------------ core
    def _open(self, url, method, body=None, timeout=None, stream=False, host=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if self.key:
            req.add_header("Authorization", "Bearer " + self.key)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if stream:
            req.add_header("Accept", "text/event-stream")
        if host:
            req.add_header("Host", host)
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
            # An HTTP status of any kind means something answered, so this is
            # never a reason to go looking for another transport.
            raise DeviceError("HTTP %d from %s: %s" % (exc.code, url, detail),
                              status=exc.code, code=code) from None
        except (socket.timeout, TimeoutError):
            raise DeviceTimeout("timed out after %ss calling %s"
                                % (timeout or self.timeout, url)) from None
        except OSError as exc:
            raise DeviceUnreachable("%s unreachable: %s" % (url, exc)) from None

    @staticmethod
    def _retryable(exc):
        """Worth trying the next transport for.

        A refused or unroutable connection, or a request that never got an
        answer at all. A DeviceTimeout carrying an HTTP status came from the
        device itself and must not send us hunting for another route.
        """
        if isinstance(exc, DeviceUnreachable):
            return True
        return isinstance(exc, DeviceTimeout) and exc.status is None

    def open(self, surface, method, path, body=None, timeout=None, stream=False):
        """Open a response, trying each route this surface allows."""
        routes = self.attempts(surface, path)
        last = None
        for index, (plane, root, host) in enumerate(routes):
            final = index == len(routes) - 1
            # Probe briefly while there is somewhere else to fall back to.
            budget = timeout or self.timeout
            if not final:
                budget = min(budget, PROBE_TIMEOUT)
            try:
                resp = self._open(root + path, method, body, budget, stream, host)
            except DeviceError as exc:
                if final or not self._retryable(exc):
                    raise
                last = exc
                continue
            transport = None
            if surface == "gateway":
                transport = TRANSPORT_VHOST if host else TRANSPORT_DIRECT
            self._remember(plane, transport)
            return resp
        raise last  # unreachable: the last attempt either returns or raises

    def call(self, surface, method, path, body=None, timeout=None):
        """One JSON call. Raises DeviceBusy on the in-band 150004."""
        resp = self.open(surface, method, path, body, timeout)
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
        return self.call("gateway", method, path, body, timeout)

    def sys(self, method, path, body=None, timeout=None):
        return self.call("mgmt", method, path, body, timeout)

    # ------------------------------------------------------------ telemetry
    def device_json(self, timeout=5):
        """Unauthenticated identity from the discovery service on 39218."""
        return self.call("discovery", "GET", "/device.json", timeout=timeout)

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
        resp = self.open("gateway", "POST",
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
        resp = self.open("gateway", "POST", "/v1/chat/completions",
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
    url = "http://%s:%d/device.json" % (bracket(address), DISCOVERY_PORT)
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def udp_probe(targets=("255.255.255.255",), timeout=1.5, port=UDP_DISCOVERY_PORT):
    """Ask every Tiiny in earshot to introduce itself, in one datagram.

    device.json advertises udp_discovery_port and discovery_token; sending the
    token to the port gets the whole of device.json straight back. Verified
    against live hardware, unicast and broadcast alike. This is the cheapest
    discovery there is: one packet, no port scan, and it finds a box whose DHCP
    address has moved.
    """
    found = []
    seen = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(timeout)
        for target in targets:
            try:
                sock.sendto(DISCOVERY_TOKEN, (target, port))
            except OSError:
                continue
        deadline = _now() + timeout
        while _now() < deadline:
            try:
                sock.settimeout(max(0.05, deadline - _now()))
                data, addr = sock.recvfrom(65535)
            except (socket.timeout, OSError):
                break
            try:
                payload = json.loads(data.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            key = (addr[0], identity(payload, addr[0]))
            if key in seen:
                continue
            seen.add(key)
            found.append((addr[0], payload))
    finally:
        sock.close()
    return found


def _now():
    import time
    return time.time()


def host_links():
    """Point-to-point links on this host that look like a Tiiny USB plane.

    Each plugged-in box gets its own interface and its own /30, so a person with
    four of them has four of these. Returns (interface, our address, the peer's
    address) and does no network traffic of its own.
    """
    out = []
    for interface, address, prefix in _interface_addresses():
        if prefix not in (30, 31) or not address.startswith(USB_NET_PREFIX):
            continue
        peer = peer_address(address, prefix)
        if peer:
            out.append((interface, address, peer))
    return out


_LINKS_CACHE = {"at": 0.0, "peers": []}
LINKS_TTL = 30.0


def host_link_peers(force=False):
    """Peer addresses this host is currently plugged into, cached briefly.

    Reading them binds 32768 sockets, and telemetry asks often, so the answer is held for
    a few seconds. The cable moving is noticed within the TTL.
    """
    now = _now()
    if force or now - _LINKS_CACHE["at"] > LINKS_TTL:
        _LINKS_CACHE["peers"] = [peer for _, _, peer in host_links()]
        _LINKS_CACHE["at"] = now
    return _LINKS_CACHE["peers"]


def usb_reachable(address):
    """Can this host actually reach that USB address right now?

    A device advertises its USB address whether or not the cable is in *this*
    machine, and an address on a /30 we are not part of does not refuse the
    connection, it hangs until it times out. Measured at six seconds. So an
    unplugged USB plane has to be deprioritised rather than tried hopefully.
    Loopback is always reachable, which is what makes the fakes work.
    """
    text = str(address or "")
    if text.startswith("127.") or text in ("::1", "localhost"):
        return True
    return text in host_link_peers()


def peer_address(address, prefix):
    """The other end of a point-to-point link.

    A /30 holds a network address, two usable addresses and a broadcast; the
    peer is whichever usable one is not ours. A /31 has no network or broadcast
    address, so both of its addresses are usable.
    """
    import struct
    try:
        packed = struct.unpack("!I", socket.inet_aton(address))[0]
    except OSError:
        return None
    mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    network = packed & mask
    candidates = [network, network + 1] if prefix == 31 else [network + 1, network + 2]
    for candidate in candidates:
        if candidate != packed:
            return socket.inet_ntoa(struct.pack("!I", candidate))
    return None


def _interface_addresses():
    """(interface, ipv4, prefix length) for every USB link this host holds.

    Standard library only, and nothing is run: a Tiiny on the cable is a
    point-to-point /30 inside 172.17/16, the box takes the first usable address
    and this machine the second, so which links are attached is settled by
    bind(), one call per usable address. bind() succeeds only on an address the
    host really holds. All 32768 of them cost about four tenths of a second on a
    laptop. The interface name is not knowable this way and is reported as
    "usb"; nothing reads it for anything but display. The farm forbids shelling
    out, which is why the ip and ifconfig readers this replaced are gone.
    """
    out = []
    for third in range(256):
        for base in range(0, 256, 4):
            for ours in (base + 2, base + 1):
                address = "%s%d.%d" % (USB_NET_PREFIX, third, ours)
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    sock.bind((address, 0))
                except OSError:
                    continue
                finally:
                    sock.close()
                out.append(("usb", address, 30))
                break
    return out


def _prefix_from_mask(mask):
    """Netmask to prefix length. ifconfig writes it as 0xfffffffc."""
    try:
        value = int(mask, 16) if mask.lower().startswith("0x") else \
            int.from_bytes(socket.inet_aton(mask), "big")
    except (ValueError, OSError):
        return None
    bits = bin(value & 0xFFFFFFFF).count("1")
    return bits


def scan(subnet, timeout=1.0, workers=64):
    """Probe every host in a /24 for a discovery responder. Opt-in only.

    `subnet` is "192.168.100" or "192.168.100.0/24". Returns a list of
    (address, device.json) pairs. The UDP probe finds the same boxes in one
    packet, so this is the fallback for a network that drops broadcast.
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


def identity(payload, address=None):
    """A stable id for a device from its device.json.

    serial_number is the field live firmware sends; the rest are tried because
    the field name was not recorded anywhere before this was checked against
    hardware. Falls back to the address so an unrecognised payload still
    registers as something.
    """
    if isinstance(payload, dict):
        for field in ("serial_number", "sn", "serial", "device_id", "deviceId", "id"):
            value = payload.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return address


def planes_from(payload, fallback_address=None):
    """Build the addressing planes a device.json describes.

    Live firmware reports ipv4_addresses as a list of {interface, address},
    which is how one box is known to be reachable on both usb0 and wlan0 at
    once. An interface this does not recognise is ignored rather than guessed
    at, and if nothing is recognised the address we actually reached is used.
    """
    planes = []
    seen = set()
    entries = (payload or {}).get("ipv4_addresses")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            address = entry.get("address")
            interface = entry.get("interface") or ""
            name = PLANE_BY_INTERFACE.get(interface)
            if name is None and str(address or "").startswith(USB_NET_PREFIX):
                name = PLANE_USB
            elif name is None:
                name = PLANE_LAN
            if not address or (name, address) in seen:
                continue
            seen.add((name, address))
            planes.append(Plane(name, address, interface=interface))
    # Keep at most one plane per name, preferring the first seen.
    unique = {}
    for plane in planes:
        unique.setdefault(plane.name, plane)
    planes = list(unique.values())
    if not planes and fallback_address:
        planes = [Plane(PLANE_LAN, fallback_address)]
    planes.sort(key=lambda p: (PLANE_PREFERENCE.index(p.name)
                               if p.name in PLANE_PREFERENCE else 9))
    return planes


def discover(subnet=None, usb=True, udp=True, timeout=1.5):
    """Find every Tiiny this host can see, deduped by serial number.

    Three sources, cheapest first:
      * a UDP broadcast, which finds anything on the LAN in one packet
      * the point-to-point links on this host, probed at the peer address
      * an opt-in /24 scan, for a network that drops broadcast

    A box on both planes answers more than once. Those answers are folded
    together by serial number, so it registers once with both addresses.
    """
    hits = []
    if udp:
        targets = ["255.255.255.255"]
        for _, ours, peer in (host_links() if usb else []):
            targets.append(peer)
        hits.extend(udp_probe(tuple(targets), timeout=timeout))
    if usb:
        for interface, ours, peer in host_links():
            payload = probe(peer, timeout=timeout)
            if payload:
                hits.append((peer, payload))
    if subnet:
        hits.extend(scan(subnet, timeout=min(timeout, 1.0)))

    records = {}
    for address, payload in hits:
        serial = identity(payload, address)
        record = records.get(serial)
        if record is None:
            record = {"serial": serial,
                      "name": payload.get("device_name") or payload.get("hostname")
                              or serial,
                      "device": payload, "seen_at": [], "planes": []}
            records[serial] = record
        if address not in record["seen_at"]:
            record["seen_at"].append(address)
        for plane in planes_from(payload, address):
            if not any(p.name == plane.name for p in record["planes"]):
                record["planes"].append(plane)
    for record in records.values():
        record["planes"].sort(key=lambda p: (PLANE_PREFERENCE.index(p.name)
                                             if p.name in PLANE_PREFERENCE else 9))
        record["addresses"] = {p.name: p.address for p in record["planes"]}
    return sorted(records.values(), key=lambda r: r["serial"])


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


def key_checker(address, gateway=None, mgmt=None, timeout=8):
    """A verify function for find_key, against a device at this address.

    tiiny-bench does the same thing: the TiinyOS local storage holds several
    UUIDs and only one of them is the live key, so each candidate is tried
    against /api/v1/models/running and the first that answers wins. The probe
    runs through the same transport fallback as everything else, so it works on
    firmware where port 8800 is closed.
    """
    def check(candidate):
        probe = Device("probe", "probe", address, key=candidate,
                       gateway=gateway, mgmt=mgmt, timeout=timeout)
        try:
            probe.running()
            return True
        except DeviceError:
            return False
    return check
