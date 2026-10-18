"""Advertise Pocket on the LAN as _ainode-pocket._tcp, so clients such as Udder find it.

No dependency: the OS already ships a Bonjour responder, and its own tool
registers the service for as long as the child process lives (dns-sd on macOS,
avahi-publish-service on Linux). Nothing secret goes in the TXT record.
"""
from __future__ import annotations

import shutil
import socket
import subprocess

SERVICE = "_ainode-pocket._tcp"


def command(port, version, devices, which=shutil.which, host=None):
    """The argv that registers this Pocket, or None when the host has no responder tool."""
    name = "AINode Pocket on %s" % (host or socket.gethostname().split(".")[0])
    txt = ["version=%s" % version, "devices=%d" % devices]
    if which("dns-sd"):
        return ["dns-sd", "-R", name, SERVICE, "local", str(port), *txt]
    if which("avahi-publish-service"):
        return ["avahi-publish-service", name, SERVICE, str(port), *txt]
    return None


def advertise(port, version, devices):
    """Start the registration; the caller terminates the returned process on shutdown."""
    # ponytail: device count is read once at startup; re-register on device add if a client starts relying on it.
    argv = command(port, version, devices)
    if not argv:
        print("bonjour: no dns-sd or avahi-publish-service on this host, not advertising")
        return None
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        print("bonjour: could not start %s: %s" % (argv[0], exc))
        return None
    print("bonjour: advertising %s on port %d" % (SERVICE, port))
    return proc
