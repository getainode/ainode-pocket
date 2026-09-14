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
