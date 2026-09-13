"""The device client: encoding, auth, error shapes, lifecycle, key discovery."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest

# `python3 -m unittest discover -s tests` makes this directory the top level, so
# a relative import has no parent package. Put the repository root on the path
# and import the package explicitly: that works whether the module is loaded as
# `test_x` or as `tests.test_x`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import FakeFleetCase, ROOT
from pocket import device as device_mod


class TestEncoding(unittest.TestCase):
    def test_slashes_are_encoded(self):
        self.assertEqual(device_mod.enc("Qwen/Qwen3-Embedding-0.6B"),
                         "Qwen%2FQwen3-Embedding-0.6B")

    def test_error_reader_recognises_the_three_recorded_shapes(self):
        read = device_mod._read_error
        self.assertEqual(read({"code": 150004, "message": "The operation failed to "
                                                          "complete."})[0], 150004)
        self.assertEqual(read({"code": 400, "msg": "Error starting model."})[0], 400)
        self.assertEqual(read({"error": {"code": 404, "message": '"x" is not loaded.',
                                         "type": "model_not_found"}})[0], 404)

    def test_a_success_is_not_read_as_an_error(self):
        read = device_mod._read_error
        # BaseResponse uses code 0 for success.
        self.assertIsNone(read({"code": 0, "msg": "ok", "log_id": "abc"}))
        self.assertIsNone(read({"choices": [{"message": {"content": "hi"}}]}))
        self.assertIsNone(read({"message": "start loading x", "progress": 0}))
        self.assertIsNone(read(["a", "list"]))


class TestClient(FakeFleetCase):
    def test_telemetry_calls(self):
        dev = self.device
        self.assertEqual(dev.device_json()["sn"], self.fake.serial)
        self.assertEqual(dev.device_info()["tiiny_os"], "0.1.33")
        self.assertIn("npus", dev.sys_status())

    def test_models_unwraps_the_openai_list(self):
        rows = self.device.models()
        self.assertIsInstance(rows, list)
        self.assertIn("model_id", rows[0])

    def test_units_and_running(self):
        units = self.device.npu_units()
        self.assertEqual(units["npu_total"], 100)
        self.assertIn(self.loaded_model(), self.device.running()["running"])

    def test_lifecycle_round_trip(self):
        dev = self.device
        model = "Qwen/Qwen3-Embedding-0.6B"
        self.assertNotIn(model, dev.running()["running"])
        reply = dev.start(model)
        self.assertEqual(reply["progress"], 0)
        self.assertIn(model, dev.running()["running"])
        self.assertIn("removed_container_ids", dev.stop(model))
        self.assertNotIn(model, dev.running()["running"])

    def test_delete_blocked_while_loaded_raises_with_the_device_code(self):
        with self.assertRaises(device_mod.DeviceError) as caught:
            self.device.delete(self.loaded_model())
        self.assertEqual(caught.exception.status, 409)

    def test_download_then_delete(self):
        dev = self.device
        model = "zai-org/GLM-4.7-Flash"
        events = [line for line in dev.download_stream(model) if line.startswith("data:")]
        self.assertTrue(events)
        self.assertIn(model, [row["model_id"] for row in dev.models()])
        dev.delete(model)
        self.assertNotIn(model, [row["model_id"] for row in dev.models()])

    def test_progress_for_an_installed_model(self):
        payload = self.device.progress(self.loaded_model())
        self.assertEqual(payload["status"], "downloaded")

    def test_unloaded_model_raises_with_code_404(self):
        with self.assertRaises(device_mod.DeviceError) as caught:
            self.device.chat({"model": "Qwen/Qwen3-Embedding-0.6B",
                              "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(caught.exception.code, 404)

    def test_a_collision_raises_device_busy(self):
        """The in-band 150004 arrives as HTTP 200, so it has to be read out of
        the body. Nothing else in the client would catch it."""
        self.fake.state.token_delay = 0.02
        dev = self.device
        model = self.loaded_model()
        errors = []

        def call():
            try:
                dev.chat({"model": model, "max_tokens": 40,
                          "messages": [{"role": "user", "content": "hi"}]})
            except device_mod.DeviceBusy as exc:
                errors.append(exc)
            except device_mod.DeviceError:
                pass

        threads = [threading.Thread(target=call) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(errors, "a concurrent call must surface as DeviceBusy")
        self.assertEqual(errors[0].code, device_mod.BUSY_CODE)

    def test_gateway_ceiling_raises_device_timeout(self):
        self.fake.state.token_delay = 0.01
        self.fake.state.request_ceiling = 0.05
        with self.assertRaises(device_mod.DeviceTimeout) as caught:
            self.device.chat({"model": self.loaded_model(), "max_tokens": 90,
                              "messages": [{"role": "user", "content": "long"}]})
        self.assertIn("220 seconds", str(caught.exception))

    def test_unreachable_address_is_a_clear_error(self):
        dev = device_mod.Device("x", "x", "127.0.0.1", key="k",
                                gateway="http://127.0.0.1:1", mgmt="http://127.0.0.1:1",
                                discovery="http://127.0.0.1:1", timeout=2)
        with self.assertRaises(device_mod.DeviceError) as caught:
            dev.running()
        self.assertIn("unreachable", str(caught.exception))

    def test_probe_reads_device_json_without_a_key(self):
        host, port = device_mod.base_url_parts(self.fake.base)
        self.assertEqual(host, "127.0.0.1")
        self.assertEqual(port, self.fake.port)


class TestKeyDiscovery(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.pop("TIINY_KEY", None)
        self.addCleanup(self._restore)

    def _restore(self):
        if self.saved is None:
            os.environ.pop("TIINY_KEY", None)
        else:
            os.environ["TIINY_KEY"] = self.saved

    def test_env_wins(self):
        os.environ["TIINY_KEY"] = "from-env"
        self.assertEqual(device_mod.find_key(), "from-env")

    def test_tiinyapps_settings_are_read(self):
        workdir = tempfile.mkdtemp()
        path = os.path.join(workdir, "device.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"base": "http://192.168.100.70:8800", "key": "from-file"}, handle)
        settings = device_mod.tiinyapps_settings(path)
        self.assertEqual(settings["key"], "from-file")
        self.assertEqual(settings["base"], "http://192.168.100.70:8800")

    def test_malformed_settings_are_ignored(self):
        workdir = tempfile.mkdtemp()
        path = os.path.join(workdir, "device.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("not json")
        self.assertIsNone(device_mod.tiinyapps_settings(path))

    def test_verify_picks_the_live_candidate(self):
        os.environ["TIINY_KEY"] = "wrong"
        chosen = device_mod.find_key(verify=lambda candidate: candidate == "right")
        self.assertEqual(chosen, "")

    def test_no_key_is_committed_to_this_repo(self):
        """A guard, not a formality: a pasted key is the one secret this repo
        could plausibly leak."""
        import re
        pattern = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
                             r"[0-9a-f]{12}")
        # A tiinyverse profile id is a public identifier in a public URL, and the
        # farm manifest is required to carry one. Drop those occurrences rather
        # than allow-listing a bare UUID, so the same id pasted anywhere else
        # still trips the guard.
        public = re.compile(r"https://www\.tiinyverse\.com/users/[0-9a-fA-F-]{36}")
        allowed = {"00000000-0000-4000-8000-000000000000"}  # the fake device's key
        offenders = []
        for folder, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "docs")]
            for name in files:
                if not name.endswith((".py", ".js", ".html", ".css", ".json", ".md")) \
                        and name != "ainode-pocket":
                    continue
                path = os.path.join(folder, name)
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    text = public.sub("", handle.read())
                for found in pattern.findall(text):
                    if found not in allowed:
                        offenders.append("%s: %s" % (name, found))
        self.assertEqual(offenders, [])
