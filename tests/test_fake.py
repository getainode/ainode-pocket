"""The fake device must reproduce the recorded shapes, or nothing built on it
means anything.

Each assertion here points at where the shape came from:
  spec-8800.json   the gateway's own OpenAPI document
  RUNBOOK.md       live-verified bodies for the free-form responses
  CAPABILITIES.md  the measured behaviour
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request

# `python3 -m unittest discover -s tests` makes this directory the top level, so
# a relative import has no parent package. Put the repository root on the path
# and import the package explicitly: that works whether the module is loaded as
# `test_x` or as `tests.test_x`.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import FakeFleetCase
from pocket import fake as fake_mod


def get(base, path, key=fake_mod.KEY):
    req = urllib.request.Request(base + path, method="GET")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode())


def post(base, path, body=None, key=fake_mod.KEY):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method="POST")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode() or "{}")
        finally:
            exc.close()


class TestRecordedShapes(FakeFleetCase):
    def test_device_json_is_unauthenticated(self):
        # RUNBOOK.md: port 39218 serves /device.json with no credential at all.
        status, payload = get(self.fake.base, "/device.json", key=None)
        self.assertEqual(status, 200)
        # Verified against live firmware 2026-09-13: serial_number is the field,
        # and the device reports every plane it answers on.
        self.assertEqual(payload["serial_number"], self.fake.serial)
        self.assertEqual(payload["discovery_token"], "GADGET_DISCOVER_V1")
        self.assertEqual(payload["service"]["udp_discovery_port"], 39217)
        interfaces = {row["interface"] for row in payload["ipv4_addresses"]}
        self.assertEqual(interfaces, {"usb0", "wlan0"})
        self.assertEqual(payload["usb"]["network"], "172.17.7.176/30")

    def test_device_info_is_unauthenticated(self):
        # API.md marks GET /api/v1/sys/device_info as the one open management route.
        status, payload = get(self.fake.base, "/api/v1/sys/device_info", key=None)
        self.assertEqual(status, 200)
        self.assertEqual(payload["tiiny_os"], "0.1.33")

    def test_authentication_is_required_elsewhere(self):
        req = urllib.request.Request(self.fake.base + "/api/v1/models/", method="GET")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()

    def test_models_list_is_an_openai_model_list(self):
        # spec-8800.json: OpenAIModelList {"object": "list", "data": [OpenAIModel]}
        # with OpenAIModel requiring name, fullname, size, params, type, id.
        status, payload = get(self.fake.base, "/api/v1/models/")
        self.assertEqual(status, 200)
        self.assertEqual(payload["object"], "list")
        self.assertTrue(payload["data"])
        for row in payload["data"]:
            for field in ("name", "fullname", "size", "params", "type", "id"):
                self.assertIn(field, row)
            self.assertEqual(row["object"], "model")
            self.assertIn("model_id", row)
            self.assertIn("npu_usage", row)

    def test_running_shape(self):
        # RUNBOOK.md and tiiny-hud: {"running": [...],
        #                            "instances": {"running": [{model_id, port, npu_usage}]}}
        status, payload = get(self.fake.base, "/api/v1/models/running")
        self.assertEqual(status, 200)
        self.assertIsInstance(payload["running"], list)
        instance = payload["instances"]["running"][0]
        self.assertIn("model_id", instance)
        self.assertIn("port", instance)
        self.assertIn("npu_usage", instance)

    def test_npu_units_shape(self):
        # RUNBOOK.md, verified live:
        # {"npu_total":100,"npu_used":50,"npu_available":50,"models":[...]}
        status, payload = get(self.fake.base, "/api/v1/models/npu/status")
        self.assertEqual(status, 200)
        self.assertEqual(payload["npu_total"], 100)
        self.assertEqual(payload["npu_used"] + payload["npu_available"], 100)
        self.assertIsInstance(payload["models"], list)
        # CAPABILITIES.md: a 35B model costs 50 units.
        self.assertEqual(payload["npu_used"], 50)

    def test_npu_status_response_shape(self):
        # spec-8800.json NpuStatusResponse, returned by /api/v1/npu/status.
        status, payload = get(self.fake.base, "/api/v1/npu/status")
        self.assertEqual(status, 200)
        for field in ("hm_smi_available", "devices", "cpu", "memory", "occupants", "npu"):
            self.assertIn(field, payload)
        entry = payload["devices"][0]
        for field in ("device_id", "util_percent", "mem_used_mb", "mem_total_mb",
                      "temp_c", "power_w", "sn"):
            self.assertIn(field, entry)
        # No temperature has ever been observed on this firmware, so the fake
        # does not invent one. Pocket must handle null rather than show a zero.
        self.assertIsNone(entry["temp_c"])
        self.assertIsNone(entry["power_w"])

    def test_sys_status_shape(self):
        status, payload = get(self.fake.base, "/api/v1/sys/status")
        self.assertEqual(status, 200)
        self.assertIn("per_core_percent", payload["cpu"])
        self.assertIn("usage_percent", payload["memory"])
        self.assertIn("total_bytes", payload["disk"])
        npus = payload["npus"][0]
        self.assertIn("memory_used_mb", npus)
        self.assertIn("memory_total_mb", npus)
        # RUNBOOK.md: the gpu block is placeholder telemetry in this firmware.
        self.assertEqual(payload["gpu"]["memory_total_mb"], 0)

    def test_catalog_is_a_bare_array(self):
        status, payload = get(self.fake.base, "/api/v1/models/online_models")
        self.assertEqual(status, 200)
        self.assertIsInstance(payload, list)
        self.assertIn("model_id", payload[0])

    def test_storage_shape(self):
        # Verified 2026-09-13: the real response wraps the body in a
        # success/data envelope. The version inferred from prose did not, and
        # nothing in Pocket reads this endpoint, so only checking it against
        # hardware could have caught that.
        status, payload = get(self.fake.base, "/api/v1/models/storage")
        self.assertEqual(status, 200)
        self.assertIs(payload["success"], True)
        data = payload["data"]
        self.assertIn("total_size_bytes", data)
        self.assertIn("model_count", data)
        self.assertIn("size_bytes", data["models"][0])

    def test_start_and_stop_replies(self):
        model = "Qwen/Qwen3-Embedding-0.6B"
        quoted = urllib.parse.quote(model, safe="")
        status, payload = post(self.fake.base, "/api/v1/models/%s/start" % quoted)
        self.assertEqual(status, 200)
        # RUNBOOK.md: {"message":"start loading <id>","progress":0}
        self.assertEqual(payload, {"message": "start loading %s" % model, "progress": 0})
        status, payload = post(self.fake.base, "/api/v1/models/%s/stop" % quoted)
        self.assertEqual(status, 200)
        # RUNBOOK.md: stop confirms the runtime is a container.
        self.assertIn("removed_container_ids", payload)

    def test_model_ids_must_be_url_encoded(self):
        # API.md calls the unencoded slash the single most common cause of
        # spurious 404s, so the fake refuses it rather than hiding it.
        status, _ = post(self.fake.base,
                         "/api/v1/models/Qwen/Qwen3-Embedding-0.6B/start")
        self.assertEqual(status, 404)

    def test_npu_budget_refuses_an_overcommit(self):
        # CAPABILITIES.md: residency is capped at 100 units. Ornith (50) plus the
        # Coder Turbo (45) leaves 5, so a 30-unit model cannot fit.
        self.fake.state.installed.append("openai/gpt-oss-20b")
        status, _ = post(
            self.fake.base, "/api/v1/models/%s/start"
            % urllib.parse.quote("Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo", safe=""))
        self.assertEqual(status, 200)
        status, payload = post(
            self.fake.base,
            "/api/v1/models/%s/start"
            % urllib.parse.quote("openai/gpt-oss-20b", safe=""))
        self.assertEqual(status, 400)
        self.assertIn("NPU units", payload["detail"])

    def test_inference_against_an_unloaded_model(self):
        # RUNBOOK.md: models do not auto-load, and this is the exact body.
        status, payload = post(self.fake.base, "/v1/chat/completions", {
            "model": "Qwen/Qwen3-Embedding-0.6B", "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["type"], "model_not_found")
        self.assertIn("is not loaded", payload["error"]["message"])

    def test_chat_completion_carries_a_timings_block(self):
        status, payload = post(self.fake.base, "/v1/chat/completions", {
            "model": self.loaded_model(), "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertTrue(payload["choices"][0]["message"]["content"])
        # API.md: the response includes real throughput, which tiiny-bench reads.
        for field in ("prompt_per_second", "predicted_per_second", "prompt_ms"):
            self.assertIn(field, payload["timings"])
        self.assertIn("completion_tokens", payload["usage"])

    def test_streaming_splits_reasoning_from_content(self):
        body = json.dumps({"model": self.loaded_model(), "max_tokens": 12,
                           "stream": True,
                           "messages": [{"role": "user", "content": "hi"}]}).encode()
        req = urllib.request.Request(self.fake.base + "/v1/chat/completions",
                                     data=body, method="POST")
        req.add_header("Authorization", "Bearer " + fake_mod.KEY)
        req.add_header("Content-Type", "application/json")
        reasoning, content, done = "", "", False
        with urllib.request.urlopen(req, timeout=20) as resp:
            for raw in resp:
                line = raw.decode().strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    done = True
                    break
                frame = json.loads(payload)
                delta = frame["choices"][0]["delta"]
                reasoning += delta.get("reasoning_content") or ""
                content += delta.get("content") or ""
        self.assertTrue(done)
        self.assertTrue(reasoning, "reasoning models split output into reasoning_content")
        self.assertTrue(content)

    def test_download_stream_reports_progress_then_installs(self):
        model = "openai/gpt-oss-20b"
        quoted = urllib.parse.quote(model, safe="")
        body = b""
        req = urllib.request.Request(
            self.fake.base + "/api/v1/models/%s/download/stream" % quoted,
            data=body, method="POST")
        req.add_header("Authorization", "Bearer " + fake_mod.KEY)
        seen = []
        with urllib.request.urlopen(req, timeout=20) as resp:
            for raw in resp:
                line = raw.decode().strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                seen.append(json.loads(payload))
        self.assertTrue(seen)
        self.assertEqual(seen[-1]["status"], "downloaded")
        self.assertEqual(seen[-1]["progress"], 100.0)
        status, payload = get(self.fake.base, "/api/v1/models/")
        self.assertIn(model, [row["model_id"] for row in payload["data"]])

    def test_a_loaded_model_cannot_be_deleted(self):
        quoted = urllib.parse.quote(self.loaded_model(), safe="")
        req = urllib.request.Request(self.fake.base + "/api/v1/models/" + quoted,
                                     method="DELETE")
        req.add_header("Authorization", "Bearer " + fake_mod.KEY)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=10)
        # spec-8800.json declares 409 "Model delete is blocked."
        self.assertEqual(caught.exception.code, 409)
        caught.exception.close()


class TestRecordedFailures(FakeFleetCase):
    """The two failures every caller meets on this hardware."""

    def test_a_second_inference_collides_with_150004(self):
        # CAPABILITIES.md: the NPU does not batch, and the collision arrives as
        # HTTP 200 with an in-band error body rather than an HTTP error.
        self.fake.state.token_delay = 0.02
        model = self.loaded_model()
        replies = []

        def call():
            replies.append(post(self.fake.base, "/v1/chat/completions", {
                "model": model, "max_tokens": 40,
                "messages": [{"role": "user", "content": "hi"}]}))

        threads = [threading.Thread(target=call) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        busy = [payload for status, payload in replies
                if status == 200 and payload.get("code") == 150004]
        self.assertTrue(busy, "the fake must collide the way the device does")
        self.assertEqual(busy[0]["message"], "The operation failed to complete.")
        self.assertTrue(self.fake.state.collisions)

    def test_the_gateway_ceiling_is_a_504(self):
        # tiiny-bug-log #042: measured 222.3s, HTTP 504, 580 tokens produced.
        # Scaled down here so the test runs in well under a second.
        self.fake.state.token_delay = 0.01
        self.fake.state.request_ceiling = 0.05
        status, _ = post(self.fake.base, "/v1/chat/completions", {
            "model": self.loaded_model(), "max_tokens": 90,
            "messages": [{"role": "user", "content": "write at length"}]})
        self.assertEqual(status, 504)
