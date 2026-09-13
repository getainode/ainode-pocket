"""The fleet: registry on disk, telemetry, the model index, routing, the lock."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest

# `python3 -m unittest discover -s tests` makes this directory the top level, so
# a relative import has no parent package. Put the repository root on the path
# and import the package explicitly: that works whether the module is loaded as
# `test_x` or as `tests.test_x`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import FakeFleetCase
from pocket import fake as fake_mod
from pocket.fleet import DeviceLock, Fleet, NoDevice, Registry, lock_identity, lock_path


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp()
        self.path = os.path.join(self.workdir, "devices.json")

    def test_round_trip_and_permissions(self):
        registry = Registry(self.path)
        registry.add({"id": "a", "name": "one", "address": "10.0.0.1", "key": "secret"})
        registry.add({"id": "b", "name": "two", "address": "10.0.0.2"})
        self.assertEqual(len(Registry(self.path).entries), 2)
        # The file can hold a device key, so it must not be world readable.
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_add_replaces_by_id(self):
        registry = Registry(self.path)
        registry.add({"id": "a", "name": "first", "address": "10.0.0.1"})
        registry.add({"id": "a", "name": "second", "address": "10.0.0.9"})
        self.assertEqual(len(registry.entries), 1)
        self.assertEqual(registry.entries[0]["name"], "second")

    def test_remove(self):
        registry = Registry(self.path)
        registry.add({"id": "a", "name": "one", "address": "10.0.0.1"})
        self.assertTrue(registry.remove("a"))
        self.assertFalse(registry.remove("a"))

    def test_a_corrupt_file_does_not_take_the_app_down(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        self.assertEqual(Registry(self.path).entries, [])


class TestLock(unittest.TestCase):
    def test_identity_collapses_spellings_of_one_host(self):
        self.assertEqual(lock_identity("localhost"), lock_identity("127.0.0.1"))
        self.assertEqual(lock_identity("127.0.0.1."), lock_identity("127.0.0.1"))

    def test_path_follows_onelane_naming(self):
        """Keeping OneLane's file name is what makes a Pocket process and a
        OneLane process share one lock instead of silently ignoring each other."""
        path = lock_path("127.0.0.1")
        self.assertTrue(os.path.basename(path).startswith("turnstile-"))
        self.assertTrue(path.endswith(".lock"))
        self.assertEqual(lock_path("localhost"), path)

    def test_one_holder_at_a_time(self):
        lock = DeviceLock("127.0.0.1")
        order = []

        def worker(tag):
            with lock.turn(why="test"):
                order.append("in:" + tag)
                time.sleep(0.05)
                order.append("out:" + tag)

        threads = [threading.Thread(target=worker, args=(str(i),)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # Every enter is immediately followed by its own exit: no overlap.
        for index in range(0, len(order), 2):
            self.assertEqual(order[index].split(":")[1], order[index + 1].split(":")[1])

    def test_queue_depth_is_visible(self):
        lock = DeviceLock("127.0.0.1")
        seen = []
        released = threading.Event()

        def holder():
            with lock.turn():
                released.wait(2)

        def waiter():
            with lock.turn():
                pass

        first = threading.Thread(target=holder)
        first.start()
        time.sleep(0.1)
        self.assertTrue(lock.busy)
        others = [threading.Thread(target=waiter) for _ in range(3)]
        for thread in others:
            thread.start()
        time.sleep(0.2)
        seen.append(lock.waiting)
        released.set()
        first.join()
        for thread in others:
            thread.join()
        self.assertEqual(seen[0], 3)
        self.assertFalse(lock.busy)

    def test_the_record_is_written_where_onelane_reads_it(self):
        lock = DeviceLock("127.0.0.1", owner="test-owner")
        with lock.turn(why="a reason"):
            with open(lock.path, "r", encoding="utf-8") as handle:
                record = json.loads(handle.read(512).strip() or "{}")
        self.assertEqual(record.get("owner"), "test-owner")
        self.assertEqual(record.get("why"), "a reason")
        self.assertEqual(record.get("pid"), os.getpid())


class TestTelemetry(FakeFleetCase):
    def test_shape(self):
        payload = self.fleet.telemetry(self.fake.serial, force=True)
        self.assertTrue(payload["online"])
        self.assertEqual(payload["npu_units"]["total"], 100)
        self.assertEqual(payload["npu_units"]["used"], 50)
        self.assertGreater(payload["npu_memory"]["total_mb"], 0)
        self.assertGreater(payload["storage"]["total_bytes"], 0)
        self.assertEqual(payload["cpu"]["cores"], 12)
        self.assertIn(self.loaded_model(), payload["running"])
        self.assertEqual(payload["instances"][0]["port"], 9098)
        self.assertEqual(payload["firmware"]["tiiny_os"], "0.1.33")

    def test_thermals_are_absent_and_say_so(self):
        """The device declares temp_c but never fills it in, so Pocket reports
        nothing rather than a zero, and marks the shape unverified."""
        payload = self.fleet.telemetry(self.fake.serial, force=True)
        thermal = payload["thermal"]
        self.assertFalse(thermal["available"])
        self.assertFalse(thermal["verified"])
        self.assertIn("no temperature", thermal["reason"])

    def test_offline_device_reports_the_error_not_a_crash(self):
        fleet = Fleet(Registry(os.path.join(self.workdir, "other.json")))
        fleet.register("127.0.0.1", key="k", name="dead", device_id="dead",
                       gateway="http://127.0.0.1:1", mgmt="http://127.0.0.1:1",
                       discovery="http://127.0.0.1:1")
        payload = fleet.telemetry("dead", force=True)
        self.assertFalse(payload["online"])
        self.assertIn("unreachable", payload["error"])

    def test_cache_spares_the_device(self):
        before = self.fleet.telemetry(self.fake.serial, force=True)
        self.fake.state.loaded = []
        again = self.fleet.telemetry(self.fake.serial)
        self.assertEqual(before["running"], again["running"])
        fresh = self.fleet.telemetry(self.fake.serial, force=True)
        self.assertEqual(fresh["running"], [])


class TestRouting(FakeFleetCase):
    devices = 3

    def test_index_is_the_union(self):
        index = self.fleet.index(force=True)
        # fake 2 installs GLM, fake 1 and 3 do not.
        self.assertIn("zai-org/GLM-4.7-Flash", index)
        self.assertEqual(index["zai-org/GLM-4.7-Flash"]["devices"],
                         [self.fakes[1].serial])
        ornith = index["deepreinforce-ai/Ornith-1.0-35B"]
        self.assertEqual(len(ornith["devices"]), 2)
        self.assertEqual(ornith["loaded_on"], [self.fakes[0].serial])

    def test_route_picks_a_device_that_has_it_loaded(self):
        dev, lock = self.fleet.route("deepreinforce-ai/Ornith-1.0-35B", force=True)
        self.assertEqual(dev.id, self.fakes[0].serial)
        self.assertIsNotNone(lock)

    def test_route_spreads_across_the_fleet_by_queue_depth(self):
        """Two devices have the Coder loaded in this spread only if we load it,
        so load it on the third and check the idle one wins."""
        model = "Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo"
        self.fakes[2].state.installed.append(model)
        self.fakes[2].state.loaded = [model]
        self.fleet.invalidate()
        busy = self.fleet.locks[self.fakes[1].serial]
        fd = busy.acquire(why="pretend")
        try:
            dev, _ = self.fleet.route(model, force=True)
            self.assertEqual(dev.id, self.fakes[2].serial)
        finally:
            busy.release(fd)

    def test_unknown_model_names_what_the_fleet_does_have(self):
        with self.assertRaises(NoDevice) as caught:
            self.fleet.route("nobody/has-this", force=True)
        message = str(caught.exception)
        self.assertIn("nobody/has-this", message)
        self.assertIn("Installed across the fleet", message)

    def test_installed_but_unloaded_names_the_holder(self):
        with self.assertRaises(NoDevice) as caught:
            self.fleet.route("zai-org/GLM-4.7-Flash", force=True)
        message = str(caught.exception)
        self.assertIn(self.fakes[1].serial, message)
        self.assertIn("not loaded", message)

    def test_empty_fleet_says_so(self):
        empty = Fleet(Registry(os.path.join(self.workdir, "empty.json")))
        with self.assertRaises(NoDevice) as caught:
            empty.route("anything")
        self.assertIn("no devices registered", str(caught.exception))


class TestRegistration(FakeFleetCase):
    def test_register_uses_the_serial_from_device_json(self):
        extra = fake_mod.FakeDevice(index=9).start()
        self.addCleanup(extra.stop)
        dev = self.fleet.register(extra.host, key=extra.key, name=extra.name,
                                 gateway=extra.base, mgmt=extra.base,
                                 discovery=extra.base, device_id=extra.serial)
        self.assertEqual(dev.id, extra.serial)
        self.assertIn(extra.serial, self.fleet.devices)
        self.assertIn(extra.serial, self.fleet.locks)

    def test_forget_removes_it_from_disk_too(self):
        self.assertTrue(self.fleet.forget(self.fake.serial))
        self.assertNotIn(self.fake.serial, self.fleet.devices)
        self.assertEqual(Registry(self.fleet.registry.path).entries, [])
