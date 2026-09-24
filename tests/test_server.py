"""The HTTP surface: the UI's JSON API, the endpoint, static assets, the bench."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# `python3 -m unittest discover -s tests` makes this directory the top level, so
# a relative import has no parent package. Put the repository root on the path
# and import the package explicitly: that works whether the module is loaded as
# `test_x` or as `tests.test_x`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import FakeFleetCase
from pocket import bench as bench_mod
from pocket import device as device_mod
from pocket import server as server_mod


class ServerCase(FakeFleetCase):
    def setUp(self):
        super().setUp()
        self.app = server_mod.App(self.fleet, self.fakes)
        self.httpd = server_mod.serve(self.app, "127.0.0.1", 0)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop_server)

    def _stop_server(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def request(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw.strip().startswith(("{", "["))
                                     else raw)
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode()
                return exc.code, (json.loads(raw) if raw.strip().startswith("{") else raw)
            finally:
                exc.close()


class TestRoutes(ServerCase):
    def test_healthz_is_what_the_farm_manifest_declares(self):
        status, payload = self.request("/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertIn("version", payload)

    def test_the_page_and_its_assets_are_served(self):
        for path, needle in (("/", "AINode Pocket"), ("/app.css", "--green: #76b900"),
                             ("/app.js", "AINode Pocket UI"),
                             ("/favicon.svg", "<svg")):
            status, body = self.request(path)
            self.assertEqual(status, 200, path)
            self.assertIn(needle, body)

    def test_the_footer_says_where_it_was_made(self):
        status, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("Made in Texas", body)
        # The silhouette is inline, sized, and hidden from screen readers so the
        # text next to it is the only thing announced.
        self.assertIn('class="texas"', body)
        self.assertIn('aria-hidden="true"', body)
        self.assertIn('fill="currentColor"', body)
        self.assertIn('width="14"', body)
        self.assertNotIn("argentos", body)

    def test_a_missing_route_is_a_json_404(self):
        status, payload = self.request("/nope")
        self.assertEqual(status, 404)
        self.assertIn("no route", payload["error"]["message"])

    def test_no_path_traversal_out_of_the_web_directory(self):
        status, _ = self.request("/../pocket/device.py")
        self.assertIn(status, (400, 404))


class TestStateApi(ServerCase):
    devices = 2

    def test_state_carries_everything_the_page_paints(self):
        status, payload = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(payload["summary"]["devices"], 2)
        self.assertEqual(payload["summary"]["online"], 2)
        self.assertTrue(payload["endpoint"]["base_url"].endswith("/v1"))
        device = payload["devices"][0]
        for field in ("npu_units", "npu_memory", "storage", "running", "lock", "thermal"):
            self.assertIn(field, device)

    def test_models_for_one_device(self):
        status, payload = self.request(
            "/api/models?device=" + self.fakes[0].serial)
        self.assertEqual(status, 200)
        self.assertTrue(payload["models"])
        self.assertIn("loaded", payload["models"][0])

    def test_models_without_a_device_is_the_union(self):
        status, payload = self.request("/api/models")
        self.assertEqual(status, 200)
        ids = [row["model_id"] for row in payload["models"]]
        self.assertIn("zai-org/GLM-4.7-Flash", ids)

    def test_catalog_marks_what_is_installed(self):
        status, payload = self.request("/api/catalog?device=" + self.fakes[0].serial)
        self.assertEqual(status, 200)
        installed = [row for row in payload["catalog"] if row["installed"]]
        self.assertTrue(installed)
        self.assertTrue(any(not row["installed"] for row in payload["catalog"]))

    def test_an_unknown_device_is_a_clean_error(self):
        status, payload = self.request("/api/models?device=nope")
        self.assertEqual(status, 502)
        self.assertIn("no device registered", payload["error"]["message"])


class TestDeviceApi(ServerCase):
    def test_discover_one_address(self):
        status, payload = self.request("/api/discover", "POST",
                                      {"address": "127.0.0.1"})
        # Nothing runs on the real discovery port during tests, so this is the
        # honest negative: a clear 404 naming the URL it tried.
        self.assertEqual(status, 404)
        self.assertIn("device.json", payload["error"]["message"])

    def test_discover_needs_to_be_told_what_to_do(self):
        """An empty body is a mistake, not a licence to broadcast.

        The sweep sends a UDP broadcast across the network the machine is on, so
        it happens when it is asked for and not by accident.
        """
        status, payload = self.request("/api/discover", "POST", {})
        self.assertEqual(status, 400)
        self.assertIn("address or a subnet", payload["error"]["message"])
        self.assertIn("auto", payload["error"]["message"])

    def test_a_bad_subnet_is_rejected_before_scanning(self):
        status, payload = self.request("/api/discover", "POST",
                                      {"subnet": "not-a-subnet"})
        self.assertEqual(status, 400)
        self.assertIn("192.168.100", payload["error"]["message"])

    def test_add_then_forget(self):
        from pocket import fake as fake_mod
        extra = fake_mod.FakeDevice(index=7).start()
        self.addCleanup(extra.stop)
        status, payload = self.request("/api/devices", "POST", {
            "address": extra.host, "key": extra.key, "name": extra.name,
            "gateway": extra.base, "mgmt": extra.base, "discovery": extra.base})
        self.assertEqual(status, 200)
        device_id = payload["device"]["id"]
        self.assertTrue(payload["telemetry"]["online"])
        status, payload = self.request("/api/devices?id=" + device_id, "DELETE")
        self.assertEqual(status, 200)
        self.assertTrue(payload["removed"])

    def test_add_requires_an_address(self):
        status, payload = self.request("/api/devices", "POST", {})
        self.assertEqual(status, 400)
        self.assertIn("address is required", payload["error"]["message"])

    def test_add_device_falls_back_to_a_password_when_no_key_is_found(self):
        from pocket import fake as fake_mod
        extra = fake_mod.FakeDevice(index=8).start()
        self.addCleanup(extra.stop)
        real_find = device_mod.find_key
        real_auth = device_mod.account_auth_key
        device_mod.find_key = lambda verify=None: ""
        device_mod.account_auth_key = lambda address, serial, password: extra.key
        self.addCleanup(lambda: setattr(device_mod, "find_key", real_find))
        self.addCleanup(lambda: setattr(device_mod, "account_auth_key", real_auth))
        status, payload = self.request("/api/devices", "POST", {
            "address": extra.host, "password": "x",
            "gateway": extra.base, "mgmt": extra.base, "discovery": extra.base})
        self.assertEqual(status, 200)
        self.assertTrue(payload["telemetry"]["online"])

    def test_unlock_adopts_the_key_the_account_api_returns(self):
        # The "Unlock" button on a red card: no key or password known yet,
        # just what the account API hands back for this password.
        real = device_mod.account_auth_key
        device_mod.account_auth_key = lambda address, serial, password: self.fake.key
        self.addCleanup(lambda: setattr(device_mod, "account_auth_key", real))
        self.fleet.devices[self.fake.serial].key = "stale-key"
        status, payload = self.request("/api/devices/unlock", "POST",
                                      {"id": self.fake.serial, "password": "x"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["telemetry"]["online"])
        self.assertEqual(self.fleet.devices[self.fake.serial].key, self.fake.key)

    def test_unlock_requires_id_and_password(self):
        status, payload = self.request("/api/devices/unlock", "POST", {"id": self.fake.serial})
        self.assertEqual(status, 400)
        self.assertIn("required", payload["error"]["message"])

    def test_unlock_a_wrong_password_is_a_clean_400(self):
        real = device_mod.account_auth_key
        device_mod.account_auth_key = lambda address, serial, password: ""
        self.addCleanup(lambda: setattr(device_mod, "account_auth_key", real))
        status, payload = self.request("/api/devices/unlock", "POST",
                                      {"id": self.fake.serial, "password": "wrong"})
        self.assertEqual(status, 400)
        self.assertIn("password", payload["error"]["message"])


class TestModelActions(ServerCase):
    def test_load_then_unload(self):
        model = "Qwen/Qwen3-Embedding-0.6B"
        status, payload = self.request("/api/models/load", "POST",
                                       {"device": self.fake.serial, "model": model})
        self.assertEqual(status, 200)
        self.assertIn("start loading", payload["result"]["message"])
        self.assertIn(model, self.fake.state.loaded)
        status, payload = self.request("/api/models/unload", "POST",
                                       {"device": self.fake.serial, "model": model})
        self.assertEqual(status, 200)
        self.assertIn("removed_container_ids", payload["result"])

    def test_delete_while_loaded_reports_the_device_refusal(self):
        status, payload = self.request("/api/models/delete", "POST",
                                       {"device": self.fake.serial,
                                        "model": self.loaded_model()})
        self.assertEqual(status, 502)
        self.assertIn("blocked", payload["error"]["message"])

    def test_download_progress(self):
        model = "openai/gpt-oss-20b"
        status, _ = self.request("/api/models/download", "POST",
                                 {"device": self.fake.serial, "model": model})
        self.assertEqual(status, 200)
        status, payload = self.request(
            "/api/models/progress?device=%s&model=%s"
            % (self.fake.serial, urllib.parse.quote(model, safe="")))
        self.assertEqual(status, 200)
        self.assertIn(payload["status"], ("downloading", "downloaded"))

    def test_download_events_stream_through(self):
        model = "zai-org/GLM-4.7-Flash"
        url = ("%s/api/models/events?device=%s&model=%s"
               % (self.base, self.fake.serial, urllib.parse.quote(model, safe="")))
        frames = []
        with urllib.request.urlopen(url, timeout=30) as resp:
            for raw in resp:
                line = raw.decode().strip()
                if line.startswith("data:"):
                    frames.append(line[5:].strip())
                if line.endswith("[DONE]"):
                    break
        self.assertTrue(frames)
        self.assertEqual(frames[-1], "[DONE]")
        self.assertIn("100", frames[-2])

    def test_an_action_needs_both_fields(self):
        status, payload = self.request("/api/models/load", "POST",
                                       {"device": self.fake.serial})
        self.assertEqual(status, 400)
        self.assertIn("device and model", payload["error"]["message"])


class TestEndpointOverHttp(ServerCase):
    def test_v1_models(self):
        status, payload = self.request("/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(payload["object"], "list")
        self.assertTrue(payload["data"])

    def test_v1_models_loaded_filter(self):
        status, payload = self.request("/v1/models?loaded=1")
        self.assertEqual(status, 200)
        for row in payload["data"]:
            self.assertTrue(row["ainode_pocket"]["ready"])

    def test_a_completion_over_http(self):
        status, payload = self.request("/v1/chat/completions", "POST", {
            "model": self.loaded_model(), "max_tokens": 16,
            "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(status, 200)
        self.assertTrue(payload["choices"][0]["message"]["content"])

    def test_a_streamed_completion_over_http(self):
        body = json.dumps({"model": self.loaded_model(), "max_tokens": 12,
                           "stream": True,
                           "messages": [{"role": "user", "content": "hi"}]}).encode()
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=body,
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        content, done = "", False
        with urllib.request.urlopen(req, timeout=30) as resp:
            self.assertIn("text/event-stream", resp.headers.get("Content-Type"))
            for raw in resp:
                line = raw.decode().strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    done = True
                    break
                frame = json.loads(payload)
                content += (frame["choices"][0]["delta"].get("content") or "")
        self.assertTrue(done)
        self.assertTrue(content)

    def test_an_unservable_model_is_a_503_over_http(self):
        status, payload = self.request("/v1/chat/completions", "POST", {
            "model": "nobody/has-this",
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["type"], "model_not_available")

    def test_a_non_json_body_is_rejected(self):
        req = urllib.request.Request(self.base + "/v1/chat/completions",
                                     data=b"not json", method="POST")
        req.add_header("Content-Type", "application/json")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()


class TestBenchApi(ServerCase):
    def test_a_short_run_saves_a_result(self):
        os.environ["AINODE_POCKET_DIR"] = self.workdir
        status, payload = self.request("/api/bench", "POST", {
            "device": self.fake.serial, "label": "unit", "only": ["prefill"]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["current"]["state"], "running")
        deadline = time.time() + 60
        current = payload["current"]
        while time.time() < deadline:
            status, payload = self.request("/api/bench")
            current = payload["current"]
            if current["state"] != "running":
                break
            time.sleep(0.3)
        self.assertEqual(current["state"], "done", current)
        self.assertTrue(payload["history"])
        saved = payload["history"][0]
        self.assertEqual(saved["label"], "unit")
        self.assertEqual(saved["model"], self.loaded_model())
        status, record = self.request("/api/bench/result?name=" + saved["saved_as"])
        self.assertEqual(status, 200)
        self.assertIn("prefill", record["results"])
        self.assertTrue(record["results"]["prefill"])
        self.assertIn("serialised through", record["notes"][0])

    def test_two_runs_at_once_are_refused(self):
        os.environ["AINODE_POCKET_DIR"] = self.workdir
        self.fake.state.token_delay = 0.01
        status, _ = self.request("/api/bench", "POST", {
            "device": self.fake.serial, "label": "first", "only": ["prefill"]})
        self.assertEqual(status, 200)
        status, payload = self.request("/api/bench", "POST", {
            "device": self.fake.serial, "label": "second", "only": ["prefill"]})
        self.assertEqual(status, 409)
        self.assertIn("already running", payload["error"]["message"])

    def test_a_run_against_a_device_with_nothing_loaded_fails_clearly(self):
        os.environ["AINODE_POCKET_DIR"] = self.workdir
        self.fake.state.loaded = []
        self.fleet.invalidate()
        run = bench_mod.start(self.fleet, self.fake.serial, "empty", ["prefill"])
        deadline = time.time() + 30
        while run.state == "running" and time.time() < deadline:
            time.sleep(0.2)
        self.assertEqual(run.state, "error")
        self.assertIn("no model loaded", run.error)

    def test_a_bad_result_name_is_refused(self):
        status, _ = self.request("/api/bench/result?name=../../etc/passwd")
        self.assertEqual(status, 404)


class TestChatPicker(ServerCase):
    """What the Chat page is allowed to put in its model picker.

    The page used to build the picker from each device's running list, which is
    every loaded model of every kind, so a loaded text-to-speech model was
    offered and answered "does not support chat" when somebody used it. The
    server decides now and the page renders what it is told.
    """

    devices = 2

    def test_the_state_offers_only_loaded_chat_models(self):
        status, payload = self.request("/api/state")
        self.assertEqual(status, 200)
        offered = [row["model_id"] for row in payload["chat_models"]]
        self.assertIn("deepreinforce-ai/Ornith-1.0-35B", offered)
        self.assertIn("Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo", offered)
        self.assertNotIn("Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", offered)
        self.assertNotIn("Qwen/Qwen3-Embedding-0.6B", offered)
        # Both of those are genuinely loaded, which is the whole point.
        running = []
        for device in payload["devices"]:
            running.extend(device["running"])
        self.assertIn("Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", running)
        self.assertIn("Qwen/Qwen3-Embedding-0.6B", running)

    def test_an_installed_chat_model_that_is_not_loaded_is_not_offered(self):
        status, payload = self.request("/api/state")
        offered = [row["model_id"] for row in payload["chat_models"]]
        self.assertNotIn("zai-org/GLM-4.7-Flash", offered,
                         "installed on fake 2, loaded nowhere, and nothing "
                         "auto-loads on this hardware")

    def test_each_offer_says_where_it_is_loaded(self):
        status, payload = self.request("/api/state")
        rows = {row["model_id"]: row for row in payload["chat_models"]}
        ornith = rows["deepreinforce-ai/Ornith-1.0-35B"]
        self.assertEqual(ornith["devices"], [self.fakes[0].serial])
        self.assertEqual(ornith["where"], self.fakes[0].name)
        self.assertEqual(ornith["type"], "Image-Text-to-Text")

    def test_a_model_loaded_on_two_devices_is_offered_once(self):
        model = "deepreinforce-ai/Ornith-1.0-35B"
        self.fakes[1].state.installed.append(model)
        self.fakes[1].state.loaded = [model]
        self.fleet.invalidate()
        status, payload = self.request("/api/state")
        rows = [row for row in payload["chat_models"] if row["model_id"] == model]
        self.assertEqual(len(rows), 1, "the endpoint routes on the model id alone")
        self.assertEqual(rows[0]["where"], "2 devices")

    def test_nothing_is_offered_when_no_chat_model_is_loaded(self):
        for fake in self.fakes:
            fake.state.loaded = ["Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
                                 if "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
                                 in fake.state.installed else
                                 "Qwen/Qwen3-Embedding-0.6B"]
        self.fleet.invalidate()
        status, payload = self.request("/api/state")
        self.assertEqual(payload["chat_models"], [])
        self.assertEqual(payload["summary"]["chat_ready"], 0)
        self.assertTrue(payload["summary"]["loaded"], "things are loaded, just not chat models")

    def test_the_page_tells_somebody_what_to_do_when_nothing_is_loaded(self):
        status, body = self.request("/app.js")
        self.assertEqual(status, 200)
        self.assertIn("No chat model is loaded", body)
        self.assertIn("Models page", body)
        # And it no longer derives the picker from the raw running list.
        self.assertIn("state.chat_models", body)
        self.assertNotIn("(device.running || []).forEach(function (modelId) {\n"
                         "      if (out.indexOf(modelId) === -1)", body)


class TestEndpointRefusesNonChatModels(ServerCase):
    devices = 2

    def test_the_http_endpoint_answers_400_not_a_device_error(self):
        status, payload = self.request("/v1/chat/completions", "POST", {
            "model": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", "max_tokens": 16,
            "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "invalid_request_error")
        self.assertIn("is a text-to-speech model and cannot chat",
                      payload["error"]["message"])
        self.assertIn("Loaded chat models:", payload["error"]["message"])

    def test_v1_models_still_lists_everything_with_a_chat_flag(self):
        status, payload = self.request("/v1/models")
        self.assertEqual(status, 200)
        rows = {row["id"]: row for row in payload["data"]}
        self.assertIn("Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", rows)
        self.assertIs(rows["Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"]
                      ["ainode_pocket"]["chat"], False)
        self.assertIs(rows["deepreinforce-ai/Ornith-1.0-35B"]
                      ["ainode_pocket"]["chat"], True)


class TestBenchPicksAChatModel(ServerCase):
    """Every test in the suite is a chat completion, so the model has to chat.

    A box commonly has an embedding or a speech model at the front of its
    running list, and the benchmark used to take whatever was first.
    """

    def test_the_speech_model_at_the_front_of_the_list_is_skipped(self):
        self.fake.state.installed.append("Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice")
        self.fake.state.loaded = ["Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
                                  "deepreinforce-ai/Ornith-1.0-35B"]
        self.fleet.invalidate()
        run = bench_mod.start(self.fleet, self.fake.serial, "kinds", ["prefill"])
        for _ in range(200):
            if run.state != "running":
                break
            time.sleep(0.05)
        self.assertEqual(run.state, "done", run.error)
        self.assertEqual(run.record["model"], "deepreinforce-ai/Ornith-1.0-35B")
        self.assertIn("model  : deepreinforce-ai/Ornith-1.0-35B", run.lines)

    def test_only_non_chat_models_loaded_is_a_clear_refusal(self):
        self.fake.state.loaded = ["Qwen/Qwen3-Embedding-0.6B"]
        self.fleet.invalidate()
        run = bench_mod.start(self.fleet, self.fake.serial, "kinds", ["prefill"])
        for _ in range(200):
            if run.state != "running":
                break
            time.sleep(0.05)
        self.assertEqual(run.state, "error")
        self.assertIn("can chat", run.error)
        self.assertIn("Qwen/Qwen3-Embedding-0.6B", run.error)


class TestBenchModelPicker(ServerCase):
    """The Bench page had a device picker and no model picker.

    It took whatever the device happened to have loaded first, so a box with an
    embedding model at the front of its running list produced a saved run whose
    every section read "failed" with the device's 400.
    """

    devices = 2

    def offered_for(self, device_id):
        """What the page's picker builds its list from, exactly as the JS does."""
        _, payload = self.request("/api/state")
        return [row["model_id"] for row in payload["chat_models"]
                if device_id in row["devices"]]

    def test_the_picker_lists_only_loaded_chat_models_on_that_device(self):
        second = self.offered_for(self.fakes[1].serial)
        self.assertEqual(second, ["Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo"])
        # That box has three models loaded; the other two cannot chat.
        _, payload = self.request("/api/state")
        running = [d["running"] for d in payload["devices"]
                   if d["id"] == self.fakes[1].serial][0]
        self.assertIn("Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", running)
        self.assertIn("Qwen/Qwen3-Embedding-0.6B", running)
        self.assertEqual(len(running), 3)

    def test_the_list_is_per_device_not_fleet_wide(self):
        first = self.offered_for(self.fakes[0].serial)
        self.assertEqual(first, ["deepreinforce-ai/Ornith-1.0-35B"])
        self.assertNotIn("Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo", first,
                         "loaded on the other box, not this one")

    def test_a_device_with_nothing_chat_capable_offers_nothing(self):
        self.fakes[0].state.loaded = ["Qwen/Qwen3-Embedding-0.6B"]
        self.fleet.invalidate()
        self.assertEqual(self.offered_for(self.fakes[0].serial), [])

    def test_the_page_disables_run_and_says_why(self):
        status, body = self.request("/app.js")
        self.assertEqual(status, 200)
        self.assertIn("benchModelsFor", body)
        self.assertIn("Nothing that can be benchmarked is loaded on this device", body)
        self.assertIn("Models page", body)
        status, body = self.request("/")
        self.assertIn('id="bench-model"', body)


class TestBenchRefusesWhatItCannotMeasure(ServerCase):
    devices = 2

    def test_an_embedding_model_is_refused_in_the_same_words_as_chat(self):
        status, payload = self.request("/api/bench", "POST", {
            "device": self.fakes[1].serial, "model": "Qwen/Qwen3-Embedding-0.6B",
            "only": ["prefill"]})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "invalid_request_error")
        self.assertIn("is an embedding model and cannot chat",
                      payload["error"]["message"])

    def test_a_speech_model_is_refused(self):
        status, payload = self.request("/api/bench", "POST", {
            "device": self.fakes[1].serial,
            "model": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", "only": ["prefill"]})
        self.assertEqual(status, 400)
        self.assertIn("is a text-to-speech model and cannot chat",
                      payload["error"]["message"])

    def test_a_refused_run_never_starts_and_never_saves(self):
        before = len(bench_mod.history())
        status, _ = self.request("/api/bench", "POST", {
            "device": self.fakes[1].serial, "model": "Qwen/Qwen3-Embedding-0.6B",
            "only": ["prefill"]})
        self.assertEqual(status, 400)
        self.assertIsNone(self.app.bench_run, "no run object was made")
        time.sleep(0.4)
        self.assertEqual(len(bench_mod.history()), before,
                         "a run that cannot work must not leave a saved result")

    def test_a_chat_model_on_the_wrong_device_is_refused(self):
        status, payload = self.request("/api/bench", "POST", {
            "device": self.fakes[0].serial,
            "model": "Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo", "only": ["prefill"]})
        self.assertEqual(status, 400)
        self.assertIn("is not loaded on", payload["error"]["message"])
        self.assertIn(self.fakes[1].serial, payload["error"]["message"])

    def test_an_unknown_model_is_refused(self):
        status, payload = self.request("/api/bench", "POST", {
            "device": self.fakes[0].serial, "model": "nobody/has-this"})
        self.assertEqual(status, 400)
        self.assertIn("no device in this fleet", payload["error"]["message"])

    def test_the_chosen_model_is_the_one_benchmarked(self):
        model = "Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo"
        status, payload = self.request("/api/bench", "POST", {
            "device": self.fakes[1].serial, "model": model, "label": "chosen",
            "only": ["prefill"]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["current"]["model"], model)
        run = self.app.bench_run
        for _ in range(200):
            if run.state != "running":
                break
            time.sleep(0.05)
        self.assertEqual(run.state, "done", run.error)
        self.assertEqual(run.record["model"], model)

    def test_no_model_still_picks_the_first_chat_capable_one(self):
        """The CLI and an older client send no model at all."""
        status, payload = self.request("/api/bench", "POST", {
            "device": self.fakes[1].serial, "label": "auto", "only": ["prefill"]})
        self.assertEqual(status, 200)
        run = self.app.bench_run
        for _ in range(200):
            if run.state != "running":
                break
            time.sleep(0.05)
        self.assertEqual(run.state, "done", run.error)
        self.assertEqual(run.record["model"], "Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo")
