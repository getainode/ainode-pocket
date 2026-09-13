"""The fleet: which devices exist, what they are doing, and who gets the next turn.

The lock is the load-bearing part. A Tiiny runs one inference at a time, so two
callers arriving together produce device error 150004 rather than a queue. The
fix is a lock per device, and the design here is lifted from OneLane
(https://tiinyapp.farm/apps/onelane/), which solved this properly first: an
advisory fcntl.flock file in a directory both applications can see, so the
kernel releases it if a holder is killed and there is no lease to expire and no
stale state to reap.

Pocket must run on the standard library alone, so the logic is vendored rather
than imported, and it keeps OneLane's file naming on purpose. A Pocket process
and a OneLane process pointed at the same device take out the same lock file and
therefore genuinely take turns. That interoperability is the reason not to
invent a new path.

Two honest limits, both OneLane's as well:
  * The lock is advisory. A program that ignores it still collides.
  * flock coordinates processes on one host. Two machines pointed at one Tiiny
    share no lock file and get no mutual exclusion at all.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import tempfile
import threading
import time

from . import device as device_mod

try:
    import fcntl
except ImportError:  # not POSIX
    fcntl = None

LOCK_MODE = 0o666
RECORD_BYTES = 512
TELEMETRY_TTL = 4.0


# --------------------------------------------------------------------- locking
def lock_dir():
    """A directory both applications can see.

    Not tempfile.gettempdir(): on macOS that is per-user, so a service account
    and a login account sharing one device would take out two different lock
    files and coordinate with nobody. OneLane's reasoning, kept verbatim in
    effect, including the ONELANE_DIR override for containers that do not share
    /tmp.
    """
    override = os.environ.get("ONELANE_DIR") or os.environ.get("TURNSTILE_DIR")
    if override:
        return override
    base = "/tmp" if os.path.isdir("/tmp") and os.access("/tmp", os.W_OK) \
        else tempfile.gettempdir()
    den = os.path.join(base, "turnstile")
    try:
        os.mkdir(den, 0o1777)
        os.chmod(den, 0o1777)
    except FileExistsError:
        pass
    except OSError:
        return base
    if os.path.isdir(den) and os.access(den, os.W_OK):
        return den
    return base


def lock_identity(host):
    """Reduce however this host was spelled to one identity per device.

    "localhost" and "127.0.0.1" are the same Tiiny but hash to two lock files,
    which is a lock that fails silently. Resolving pins both to one address; the
    literal spelling is the honest fallback when resolution fails, because
    over-serialising two names that turn out to be one device only costs
    throughput while under-serialising is a race.
    """
    name = (host or "").strip().rstrip(".").lower() or "127.0.0.1"
    try:
        return socket.gethostbyname(name)
    except (socket.gaierror, UnicodeError, OSError):
        return name


def lock_path(host):
    ident = lock_identity(host)
    safe = "".join(c if c.isalnum() else "-" for c in ident).strip("-") or "device"
    key_id = "%s-%s" % (safe[:32], hashlib.sha1(ident.encode()).hexdigest()[:8])
    return os.path.join(lock_dir(), "turnstile-%s.lock" % key_id)


class DeviceLock:
    """One turn at a time on one device.

    Inside this process a threading.Lock does the queueing and lets the router
    see how deep each queue is. Across processes an advisory flock on OneLane's
    path does the same job. If the flock cannot be taken at all the in-process
    lock still holds and `shared` says so, because a lock that quietly stops
    being shared is worse than one that admits it.
    """

    def __init__(self, host, owner="ainode-pocket"):
        self.host = host
        self.owner = owner
        self.path = lock_path(host)
        self.shared = fcntl is not None
        self.reason = None if fcntl is not None else "no fcntl on this platform"
        self._local = threading.Lock()
        self._waiting = 0
        self._guard = threading.Lock()
        self._held_since = None

    @property
    def waiting(self):
        with self._guard:
            return self._waiting

    @property
    def busy(self):
        return self._held_since is not None

    @property
    def held_for(self):
        started = self._held_since
        return None if started is None else round(time.time() - started, 2)

    def _open_shared(self):
        """Open the lock file so any user sharing the device can lock it.

        O_NOFOLLOW because the path is predictable and the directory is
        world-writable, which is the classic symlink-attack shape.
        """
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, LOCK_MODE)
        return fd

    def _write_record(self, fd, record):
        """Fixed-width write so a reader never catches a half-written record.

        OneLane's dashboard reads this file, so the shape matches what it
        expects: owner, pid, why, since.
        """
        try:
            blob = json.dumps(record, default=str).encode("utf-8")[:RECORD_BYTES]
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, blob + b" " * (RECORD_BYTES - len(blob)))
        except Exception:
            pass

    def acquire(self, why="inference", timeout=600):
        with self._guard:
            self._waiting += 1
        try:
            if not self._local.acquire(timeout=timeout):
                raise TimeoutError("waited %ss for the in-process lock on %s"
                                   % (timeout, self.host))
        finally:
            with self._guard:
                self._waiting -= 1
        fd = None
        if fcntl is not None:
            try:
                fd = self._open_shared()
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._write_record(fd, {"owner": self.owner, "pid": os.getpid(),
                                        "why": why, "since": time.time()})
            except OSError as exc:
                # Degrade to in-process only rather than refusing to serve, but
                # record why so the UI can say the lock is not shared.
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                fd = None
                self.shared = False
                self.reason = str(exc)
        self._held_since = time.time()
        return fd

    def release(self, fd):
        self._held_since = None
        if fd is not None:
            try:
                self._write_record(fd, {})
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        self._local.release()

    class _Turn:
        def __init__(self, lock, why, timeout):
            self.lock = lock
            self.why = why
            self.timeout = timeout
            self.fd = None

        def __enter__(self):
            self.fd = self.lock.acquire(self.why, self.timeout)
            return self.lock

        def __exit__(self, *exc):
            self.lock.release(self.fd)
            return False

    def turn(self, why="inference", timeout=600):
        return DeviceLock._Turn(self, why, timeout)


# -------------------------------------------------------------------- registry
def data_dir():
    """Where Pocket keeps its own state.

    The farm hands every app a private data directory in FARM_DATA_DIR, so use
    it when it is set and fall back to ~/.ainode-pocket otherwise. Nothing here
    is ever written into the repo.
    """
    chosen = os.environ.get("AINODE_POCKET_DIR") or os.environ.get("FARM_DATA_DIR")
    if not chosen:
        chosen = os.path.join(os.path.expanduser("~"), ".ainode-pocket")
    os.makedirs(chosen, mode=0o700, exist_ok=True)
    return chosen


class Registry:
    """The list of devices, on disk at 0600.

    A device entry may carry its own key, because a fleet of several Tiinys has
    several keys and ~/.tiinyapps/device.json only describes one. Keys live in
    the user's data directory, never in the repo.
    """

    def __init__(self, path=None):
        self.path = path or os.path.join(data_dir(), "devices.json")
        self._guard = threading.Lock()
        self.entries = self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return []
        entries = payload.get("devices") if isinstance(payload, dict) else payload
        return [e for e in (entries or []) if isinstance(e, dict) and e.get("id")]

    def save(self):
        with self._guard:
            tmp = self.path + ".tmp"
            blob = json.dumps({"devices": self.entries}, indent=2) + "\n"
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(blob)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)

    def add(self, entry):
        self.entries = [e for e in self.entries if e.get("id") != entry["id"]]
        self.entries.append(entry)
        self.save()
        return entry

    def remove(self, device_id):
        before = len(self.entries)
        self.entries = [e for e in self.entries if e.get("id") != device_id]
        if len(self.entries) != before:
            self.save()
            return True
        return False


class NoDevice(Exception):
    """No device can serve this request, with a reason a caller can act on."""


class Fleet:
    """Devices, their telemetry, and the routing decision.

    Telemetry is cached for a few seconds. Six panels on one page refreshing
    every two seconds would otherwise hammer a box whose whole problem is that
    it does one thing at a time.
    """

    def __init__(self, registry=None, ttl=TELEMETRY_TTL):
        self.registry = registry if registry is not None else Registry()
        self.ttl = ttl
        self.devices = {}
        self.locks = {}
        self._cache = {}
        self._guard = threading.Lock()
        for entry in self.registry.entries:
            self._attach(entry)

    # --------------------------------------------------------------- devices
    def _attach(self, entry):
        dev = device_mod.Device(
            entry["id"], entry.get("name") or entry["id"], entry.get("address", ""),
            key=entry.get("key") or device_mod.find_key(),
            gateway=entry.get("gateway"), mgmt=entry.get("mgmt"),
            discovery=entry.get("discovery"))
        self.devices[dev.id] = dev
        host = device_mod.base_url_parts(dev.gateway)[0] or dev.address or dev.id
        self.locks[dev.id] = DeviceLock(host)
        return dev

    def register(self, address, key=None, name=None, gateway=None, mgmt=None,
                 discovery=None, device_id=None):
        """Add a device. The address is enough; discovery fills in the rest."""
        payload = device_mod.probe(address) if address and not device_id else None
        resolved_id = device_id or device_mod.identity(payload, address)
        entry = {"id": resolved_id,
                 "name": name or (payload or {}).get("device_name") or address or resolved_id,
                 "address": address,
                 "added_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        if key:
            entry["key"] = key
        for field, value in (("gateway", gateway), ("mgmt", mgmt), ("discovery", discovery)):
            if value:
                entry[field] = value
        self.registry.add(entry)
        with self._guard:
            self._cache.pop(resolved_id, None)
        return self._attach(entry)

    def forget(self, device_id):
        self.devices.pop(device_id, None)
        self.locks.pop(device_id, None)
        with self._guard:
            self._cache.pop(device_id, None)
        return self.registry.remove(device_id)

    def get(self, device_id):
        dev = self.devices.get(device_id)
        if dev is None:
            raise NoDevice("no device registered with id %s" % device_id)
        return dev

    # ------------------------------------------------------------ telemetry
    def telemetry(self, device_id, force=False):
        """One device's live state, cached for `ttl` seconds.

        Thermals are reported only when the device actually sends them. The
        management API has no temperature field at all; /api/v1/npu/status
        declares temp_c and power_w per NPU device, so those are read and simply
        omitted when null. Nothing here invents a temperature.
        """
        now = time.time()
        with self._guard:
            hit = self._cache.get(device_id)
            if hit and not force and now - hit["at"] < self.ttl:
                return hit["value"]
        dev = self.get(device_id)
        value = {"id": dev.id, "name": dev.name, "address": dev.address,
                 "gateway": dev.gateway, "online": False, "error": None,
                 "lock": {"busy": False, "waiting": 0, "shared": True, "held_for": None}}
        try:
            units = dev.npu_units()
            running = dev.running()
            status = dev.sys_status()
            value["online"] = True
            total = units.get("npu_total") or 100
            used = units.get("npu_used") or 0
            value["npu_units"] = {"used": used, "total": total,
                                  "available": units.get("npu_available",
                                                          max(0, total - used)),
                                  "percent": round(used / total * 100, 1) if total else 0}
            npus = (status.get("npus") or [{}])[0] if isinstance(status.get("npus"), list) else {}
            mem_used = npus.get("memory_used_mb") or 0
            mem_total = npus.get("memory_total_mb") or 0
            value["npu_memory"] = {
                "used_mb": mem_used, "total_mb": mem_total,
                "percent": round(mem_used / mem_total * 100, 1) if mem_total else 0,
                # utilization_percent reads 0.0 even mid-generation on this
                # firmware, so it is passed through and labelled, not trusted.
                "utilization_percent": npus.get("utilization_percent")}
            disk = status.get("disk") or {}
            disk_total = disk.get("total_bytes") or 0
            disk_used = disk.get("used_bytes") or 0
            value["storage"] = {
                "used_bytes": disk_used, "total_bytes": disk_total,
                "free_bytes": max(0, disk_total - disk_used),
                "percent": round(disk_used / disk_total * 100, 1) if disk_total else 0}
            memory = status.get("memory") or {}
            value["memory"] = {"used_bytes": memory.get("used_bytes"),
                               "total_bytes": memory.get("total_bytes"),
                               "percent": memory.get("usage_percent")}
            cpu = status.get("cpu") or {}
            value["cpu"] = {"percent": cpu.get("total_percent"),
                            "cores": len(cpu.get("per_core_percent") or [])}
            value["running"] = list(running.get("running") or [])
            instances = (running.get("instances") or {}).get("running") or []
            value["instances"] = [
                {"model_id": inst.get("model_id"), "port": inst.get("port"),
                 "npu_usage": inst.get("npu_usage")}
                for inst in instances if isinstance(inst, dict)]
            value["thermal"] = self._thermal(dev)
            info = {}
            try:
                info = dev.device_info()
            except device_mod.DeviceError:
                info = {}
            value["firmware"] = {"tiiny_os": info.get("tiiny_os"),
                                 "service": info.get("version"),
                                 "model": info.get("device_model_name"),
                                 "serial": info.get("sn")}
        except device_mod.DeviceError as exc:
            value["error"] = str(exc)
        lock = self.locks.get(device_id)
        if lock is not None:
            value["lock"] = {"busy": lock.busy, "waiting": lock.waiting,
                             "shared": lock.shared, "reason": lock.reason,
                             "held_for": lock.held_for, "path": lock.path}
        with self._guard:
            self._cache[device_id] = {"at": now, "value": value}
        return value

    def _thermal(self, dev):
        """Temperatures, only where the device API provides them.

        /api/v1/npu/status declares temp_c and power_w per NPU device. On the
        firmware this was written against those fields have never been observed
        non-null, and there is no temperature anywhere else on the REST surface,
        so the shape is read but flagged unverified and the UI shows nothing
        rather than a zero.
        """
        try:
            payload = dev.npu_devices()
        except device_mod.DeviceError:
            return {"available": False, "reason": "device did not answer /api/v1/npu/status",
                    "verified": False}
        devices = payload.get("devices")
        readings = []
        if isinstance(devices, list):
            for entry in devices:
                if not isinstance(entry, dict):
                    continue
                if entry.get("temp_c") is None and entry.get("power_w") is None:
                    continue
                readings.append({"device_id": entry.get("device_id"),
                                 "temp_c": entry.get("temp_c"),
                                 "power_w": entry.get("power_w"),
                                 "model": entry.get("model")})
        return {"available": bool(readings), "readings": readings,
                "hm_smi_available": payload.get("hm_smi_available"),
                "verified": False,
                "reason": None if readings else
                          "this firmware reports no temperature on the HTTP API"}

    def overview(self, force=False):
        return [self.telemetry(device_id, force) for device_id in self.devices]

    # --------------------------------------------------------------- models
    def models(self, device_id, force=False):
        """Installed models on one device, annotated with loaded state."""
        dev = self.get(device_id)
        running = set(self.telemetry(device_id, force).get("running") or [])
        out = []
        for entry in dev.models():
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("model_id") or entry.get("id") or entry.get("name")
            if not model_id:
                continue
            out.append({"model_id": model_id,
                        "name": entry.get("display_name") or entry.get("name") or model_id,
                        "type": entry.get("type") or "",
                        "params": entry.get("params") or "",
                        "size": entry.get("size") or entry.get("total_size") or 0,
                        "npu_usage": entry.get("npu_usage") or 0,
                        "status": entry.get("status") or "",
                        "loaded": model_id in running})
        out.sort(key=lambda row: (row["type"], row["model_id"]))
        return out

    def index(self, force=False):
        """The union of models across the fleet, keyed by model id.

        This is what /v1/models is built from. Each model records which devices
        hold it and which of those have it loaded, because that distinction is
        the whole routing decision.
        """
        union = {}
        for device_id in self.devices:
            try:
                rows = self.models(device_id, force)
            except device_mod.DeviceError:
                continue
            for row in rows:
                slot = union.setdefault(row["model_id"], {
                    "model_id": row["model_id"], "type": row["type"],
                    "params": row["params"], "size": row["size"],
                    "devices": [], "loaded_on": []})
                slot["devices"].append(device_id)
                if row["loaded"]:
                    slot["loaded_on"].append(device_id)
                if not slot["type"]:
                    slot["type"] = row["type"]
        return union

    def route(self, model_id, force=False):
        """Pick a device that has this model loaded, or explain why none can.

        Ties break on the shortest queue, then on the fewest loaded models, so
        work spreads across a fleet instead of piling onto the first box.
        """
        if not self.devices:
            raise NoDevice("no devices registered. Add one on the Devices page "
                           "or with --device <address>.")
        union = self.index(force)
        slot = union.get(model_id)
        if slot is None:
            known = sorted(union)
            raise NoDevice(
                "no device in this fleet has a model called %r. Installed across "
                "the fleet: %s" % (model_id, ", ".join(known) if known else "nothing"))
        if not slot["loaded_on"]:
            holders = ", ".join(slot["devices"])
            raise NoDevice(
                "%r is installed on %s but is not loaded on any of them. Models "
                "do not auto-load on this hardware: load it on the Models page "
                "first." % (model_id, holders))
        def rank(device_id):
            lock = self.locks.get(device_id)
            waiting = lock.waiting + (1 if lock.busy else 0) if lock else 0
            loaded = len(self.telemetry(device_id).get("running") or [])
            return (waiting, loaded, device_id)
        chosen = sorted(slot["loaded_on"], key=rank)[0]
        return self.devices[chosen], self.locks[chosen]

    def invalidate(self, device_id=None):
        with self._guard:
            if device_id is None:
                self._cache.clear()
            else:
                self._cache.pop(device_id, None)
