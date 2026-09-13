"""Test support. Every test here runs against the fake device in pocket/fake.py,
so the whole suite passes with no hardware and no network beyond loopback."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pocket import fake as fake_mod  # noqa: E402
from pocket.fleet import Fleet, Registry  # noqa: E402


class FakeFleetCase(unittest.TestCase):
    """A fleet of fake devices plus a throwaway data directory.

    `devices` is how many fakes to start. Subclasses override it.
    """

    devices = 1
    fake_kwargs = {}

    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="ainode-pocket-test-")
        self._saved_env = os.environ.get("AINODE_POCKET_DIR")
        os.environ["AINODE_POCKET_DIR"] = self.workdir
        self.fakes = fake_mod.fleet_of(self.devices, **self.fake_kwargs)
        self.fleet = Fleet(Registry(os.path.join(self.workdir, "devices.json")))
        for entry in self.fakes:
            self.fleet.register(entry.host, key=entry.key, name=entry.name,
                                gateway=entry.base, mgmt=entry.base,
                                discovery=entry.base, device_id=entry.serial)
        self.addCleanup(self._teardown)

    def _teardown(self):
        for entry in self.fakes:
            entry.stop()
        if self._saved_env is None:
            os.environ.pop("AINODE_POCKET_DIR", None)
        else:
            os.environ["AINODE_POCKET_DIR"] = self._saved_env
        shutil.rmtree(self.workdir, ignore_errors=True)

    @property
    def fake(self):
        return self.fakes[0]

    @property
    def device(self):
        return self.fleet.devices[self.fakes[0].serial]

    def loaded_model(self, index=0):
        return self.fakes[index].state.loaded[0]
