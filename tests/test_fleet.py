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
from pocket import device as device_mod
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


class TestPlanes(unittest.TestCase):
    """One box, several addresses.

    A Tiiny answers on USB and Wi-Fi at the same time, each on its own
    interface, and reports both in device.json. It has to register once, not
    twice, and traffic should prefer the USB link because it is a fixed
    point-to-point /30 while the LAN address is DHCP.
    """

    def test_planes_are_read_from_the_device_s_own_address_list(self):
        payload = {"serial_number": "TNY1", "device_name": "one",
                   "ipv4_addresses": [{"interface": "usb0", "address": "172.17.7.177"},
                                      {"interface": "wlan0", "address": "192.168.100.94"}]}
        planes = device_mod.planes_from(payload)
        self.assertEqual([p.name for p in planes], ["usb", "lan"])
        self.assertEqual(planes[0].address, "172.17.7.177")
        self.assertEqual(planes[1].address, "192.168.100.94")

    def test_an_unknown_interface_becomes_a_lan_plane(self):
        payload = {"ipv4_addresses": [{"interface": "en5", "address": "10.0.0.4"}]}
        planes = device_mod.planes_from(payload)
        self.assertEqual([p.name for p in planes], ["lan"])

    def test_a_device_json_with_no_addresses_falls_back(self):
        planes = device_mod.planes_from({}, "10.0.0.9")
        self.assertEqual([(p.name, p.address) for p in planes], [("lan", "10.0.0.9")])

    def test_usb_is_preferred_when_the_cable_is_in_this_host(self):
        dev = device_mod.Device("id", "name", planes=[
            {"name": "lan", "address": "127.0.0.1"},
            {"name": "usb", "address": "127.0.0.1"}])
        # Loopback is reachable by definition, which stands in for a live cable.
        self.assertEqual(dev.active.name, "usb")
        self.assertEqual([p.name for p in dev.ordered_planes()], ["usb", "lan"])

    def test_an_unplugged_usb_plane_goes_last(self):
        """The device advertises its USB address whether or not the cable is in
        this machine, and connecting to one that is not costs a six second hang
        rather than a refusal. So it must not be tried first."""
        dev = device_mod.Device("id", "name", planes=[
            {"name": "usb", "address": "172.17.99.177"},
            {"name": "lan", "address": "127.0.0.1"}])
        self.assertEqual([p.name for p in dev.ordered_planes()], ["lan", "usb"])
        self.assertEqual(dev.active.name, "usb", "the record still keeps it")

    def test_peer_address_of_a_point_to_point_link(self):
        self.assertEqual(device_mod.peer_address("172.17.7.178", 30), "172.17.7.177")
        self.assertEqual(device_mod.peer_address("172.17.7.177", 30), "172.17.7.178")
        self.assertEqual(device_mod.peer_address("172.17.4.1", 31), "172.17.4.0")

    def test_the_host_s_own_links_can_be_enumerated(self):
        # Whatever this machine has, the shape has to be right: a /30 or /31 in
        # the USB range, with a peer that is not us.
        for interface, ours, peer in device_mod.host_links():
            self.assertTrue(ours.startswith("172.17."))
            self.assertNotEqual(ours, peer)
            self.assertTrue(interface)


class TestTwoPlanesLive(FakeFleetCase):
    """The same fake box answering on both planes at once."""

    def setUp(self):
        super().setUp()
        self.both = fake_mod.FakeDevice(index=5, planes=["usb", "lan"]).start()
        self.addCleanup(self.both.stop)
        self.dev = self.fleet.register(
            self.both.host, key=self.both.key, name=self.both.name,
            device_id=self.both.serial, planes=self.both.plane_records())

    def test_it_registers_once_with_both_addresses(self):
        self.assertEqual(len(self.dev.planes), 2)
        self.assertEqual(sorted(self.dev.addresses), ["lan", "usb"])
        self.assertEqual(self.dev.active.name, "usb")

    def test_traffic_takes_the_usb_plane(self):
        self.dev.running()
        self.assertEqual(self.dev.plane, "usb")

    def test_it_falls_back_to_lan_when_usb_goes_away(self):
        self.dev.running()
        self.assertEqual(self.dev.plane, "usb")
        self.both.stop_plane("usb")
        payload = self.dev.running()
        self.assertIn("running", payload)
        self.assertEqual(self.dev.plane, "lan")

    def test_the_route_is_written_to_the_registry(self):
        self.dev.running()
        entries = Registry(self.fleet.registry.path).entries
        saved = [e for e in entries if e["id"] == self.both.serial][0]
        self.assertEqual(saved["plane"], "usb")
        self.assertEqual(len(saved["planes"]), 2)

    def test_one_lock_covers_both_planes(self):
        """Two addresses for one NPU must not mean two lock files."""
        lock = self.fleet.locks[self.both.serial]
        other = DeviceLock(self.fleet._lock_host(self.dev))
        self.assertEqual(lock.path, other.path)
        self.assertIn(self.both.serial.lower(), os.path.basename(lock.path))

    def test_a_onelane_neighbour_is_still_held_off(self):
        """The serial-keyed lock alone would leave a neighbour free to collide.

        OneLane keys on the address, so Pocket takes that file too. Without it a
        OneLane process would take a lock nobody else holds and run straight
        into a 150004.
        """
        lock = self.fleet.locks[self.both.serial]
        neighbours = lock.neighbour_paths()
        self.assertTrue(neighbours, "the device's addresses must be locked too")
        expected = lock_path(self.dev.active.address)
        self.assertIn(expected, neighbours)

        held = lock.acquire(why="test")
        try:
            # A neighbour taking OneLane's file for this address must block.
            import fcntl
            fd = os.open(expected, os.O_RDWR | os.O_CREAT, 0o666)
            try:
                with self.assertRaises(OSError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
        finally:
            lock.release(held)

    def test_telemetry_reports_both_addresses(self):
        payload = self.fleet.telemetry(self.both.serial, force=True)
        self.assertTrue(payload["online"])
        self.assertEqual(sorted(payload["transport"]["planes"]), ["lan", "usb"])
        self.assertEqual(payload["transport"]["plane"], "usb")


class TestFourDevices(FakeFleetCase):
    """A person can own four boxes, and several can be on USB at once."""

    devices = 4

    def test_all_four_register_and_answer(self):
        self.assertEqual(len(self.fleet.devices), 4)
        overview = self.fleet.overview(force=True)
        self.assertEqual(len(overview), 4)
        self.assertTrue(all(row["online"] for row in overview))

    def test_each_device_has_its_own_lock(self):
        paths = {lock.path for lock in self.fleet.locks.values()}
        self.assertEqual(len(paths), 4, "four boxes, four locks")

    def test_the_model_index_unions_all_four(self):
        index = self.fleet.index(force=True)
        for slot in index.values():
            self.assertTrue(slot["devices"])
        every = set()
        for slot in index.values():
            every.update(slot["devices"])
        self.assertEqual(len(every), 4)

    def test_routing_reaches_a_model_loaded_on_the_fourth_box(self):
        only_here = "Qwen/Qwen3-Reranker-0.6B"
        self.fakes[3].state.installed.append(only_here)
        self.fakes[3].state.loaded.append(only_here)
        self.fleet.invalidate()
        dev, _ = self.fleet.route(only_here, force=True)
        self.assertEqual(dev.id, self.fakes[3].serial)


class TestUdpDiscovery(unittest.TestCase):
    """The one-packet discovery the device advertises in its own device.json."""

    def setUp(self):
        self.fake = fake_mod.FakeDevice(index=8).start()
        self.addCleanup(self.fake.stop)
        self.responder = fake_mod.FakeDiscovery(self.fake.state).start()
        self.addCleanup(self.responder.stop)

    def test_the_token_gets_the_whole_device_json_back(self):
        found = device_mod.udp_probe(("127.0.0.1",), timeout=2,
                                     port=self.responder.port)
        self.assertEqual(len(found), 1)
        address, payload = found[0]
        self.assertEqual(payload["serial_number"], self.fake.serial)
        self.assertEqual(payload["discovery_token"], "GADGET_DISCOVER_V1")

    def test_a_wrong_token_gets_nothing(self):
        import socket as _socket
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        sock.settimeout(1)
        try:
            sock.sendto(b"hello?", ("127.0.0.1", self.responder.port))
            with self.assertRaises(_socket.timeout):
                sock.recvfrom(2048)
        finally:
            sock.close()


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


class TestUnlock(FakeFleetCase):
    """Fleet.unlock(): a fresh key adopted in memory and on disk.

    device_mod.account_auth_key() is the real HTTP call, tested on its own in
    test_account_auth.py; here the point is what Fleet does with whatever
    that call returns.
    """
    def setUp(self):
        super().setUp()
        self._real = device_mod.account_auth_key

    def tearDown(self):
        device_mod.account_auth_key = self._real
        super().tearDown()

    def test_success_updates_key_in_memory_and_on_disk(self):
        # The fake only answers to its own real key, so use that as the
        # "freshly returned" key -- proves the round trip (adopt it, then
        # the very next telemetry read uses it) actually works, not just
        # that a string got copied around.
        device_mod.account_auth_key = lambda address, serial, password: self.fake.key
        self.fleet.devices[self.fake.serial].key = "stale-key"
        telemetry = self.fleet.unlock(self.fake.serial, "the-password")
        self.assertEqual(self.fleet.devices[self.fake.serial].key, self.fake.key)
        reloaded = Registry(self.fleet.registry.path)
        entry = next(e for e in reloaded.entries if e["id"] == self.fake.serial)
        self.assertEqual(entry["key"], self.fake.key)
        self.assertTrue(telemetry.get("online"))

    def test_wrong_password_raises_and_leaves_the_old_key(self):
        device_mod.account_auth_key = lambda address, serial, password: ""
        old_key = self.fleet.devices[self.fake.serial].key
        with self.assertRaises(NoDevice):
            self.fleet.unlock(self.fake.serial, "wrong")
        self.assertEqual(self.fleet.devices[self.fake.serial].key, old_key)

    def test_unknown_device_raises(self):
        with self.assertRaises(NoDevice):
            self.fleet.unlock("no-such-device", "x")


class TestChatCapability(FakeFleetCase):
    """What a model is for, as the device reports it."""

    devices = 2

    def test_the_union_carries_the_answer_per_model(self):
        index = self.fleet.index(force=True)
        self.assertFalse(index["Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"]["chat"])
        self.assertFalse(index["Qwen/Qwen3-Embedding-0.6B"]["chat"])
        self.assertFalse(index["Qwen/Qwen3-ASR-1.7B"]["chat"])
        self.assertTrue(index["deepreinforce-ai/Ornith-1.0-35B"]["chat"])
        self.assertTrue(index["zai-org/GLM-4.7-Flash"]["chat"])

    def test_chat_models_is_loaded_chat_models_only(self):
        loaded = self.fleet.chat_models()
        self.assertIn("deepreinforce-ai/Ornith-1.0-35B", loaded)
        self.assertIn("Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo", loaded)
        self.assertNotIn("Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", loaded)
        self.assertNotIn("zai-org/GLM-4.7-Flash", loaded, "installed, not loaded")
        every = self.fleet.chat_models(loaded_only=False)
        self.assertIn("zai-org/GLM-4.7-Flash", every)
        self.assertNotIn("Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", every)

    def test_capabilities_beat_the_type_label(self):
        """The runtime's own answer wins over the store's label."""
        self.assertTrue(device_mod.can_chat("Text-to-Speech", ["main"]))
        self.assertFalse(device_mod.can_chat("Text Generation", ["voice"]))

    def test_an_empty_capability_list_falls_back_to_the_type(self):
        self.assertTrue(device_mod.can_chat("Text Generation", []))
        self.assertTrue(device_mod.can_chat("Image-Text-to-Text", None))
        self.assertFalse(device_mod.can_chat("Text-to-Speech", []))
        self.assertFalse(device_mod.can_chat("Music Generation", []))
        self.assertFalse(device_mod.can_chat("", []))

    def test_a_type_nobody_has_seen_gets_no_invented_description(self):
        self.assertEqual(device_mod.type_phrase("Video-to-Haiku"), "")
        self.assertEqual(device_mod.type_phrase("text-to-speech"),
                         "a text-to-speech model")
        self.assertEqual(device_mod.type_phrase("ASR"),
                         "a speech recognition model")
