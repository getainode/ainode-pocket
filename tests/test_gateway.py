"""The endpoint: the union, routing, streaming, and the queue that keeps
concurrent callers from colliding."""
from __future__ import annotations

import threading

# `python3 -m unittest discover -s tests` makes this directory the top level, so
# a relative import has no parent package. Put the repository root on the path
# and import the package explicitly: that works whether the module is loaded as
# `test_x` or as `tests.test_x`.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import FakeFleetCase
from pocket import gateway


class TestModelsUnion(FakeFleetCase):
    devices = 2

    def test_union_across_devices(self):
        payload = gateway.models_payload(self.fleet)
        self.assertEqual(payload["object"], "list")
        ids = [row["id"] for row in payload["data"]]
        self.assertIn("deepreinforce-ai/Ornith-1.0-35B", ids)
        self.assertIn("zai-org/GLM-4.7-Flash", ids)
        self.assertEqual(len(ids), len(set(ids)), "a model on two devices is listed once")

    def test_each_row_says_where_it_lives_and_whether_it_is_ready(self):
        rows = {row["id"]: row for row in gateway.models_payload(self.fleet)["data"]}
        ornith = rows["deepreinforce-ai/Ornith-1.0-35B"]
        self.assertTrue(ornith["ainode_pocket"]["ready"])
        self.assertEqual(ornith["ainode_pocket"]["loaded_on"], [self.fakes[0].serial])
        glm = rows["zai-org/GLM-4.7-Flash"]
        self.assertFalse(glm["ainode_pocket"]["ready"])
        self.assertEqual(glm["ainode_pocket"]["devices"], [self.fakes[1].serial])

    def test_loaded_only_filters(self):
        rows = gateway.models_payload(self.fleet, loaded_only=True)["data"]
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue(row["ainode_pocket"]["ready"])

    def test_owned_by_names_the_device_or_counts_them(self):
        rows = {row["id"]: row for row in gateway.models_payload(self.fleet)["data"]}
        self.assertEqual(rows["zai-org/GLM-4.7-Flash"]["owned_by"], self.fakes[1].name)
        self.assertEqual(rows["Qwen/Qwen3-Embedding-0.6B"]["owned_by"], "2 devices")


class TestChat(FakeFleetCase):
    devices = 2

    def test_a_completion_says_which_device_answered(self):
        status, payload = gateway.chat(self.fleet, {
            "model": "deepreinforce-ai/Ornith-1.0-35B", "max_tokens": 24,
            "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(status, 200)
        self.assertTrue(payload["choices"][0]["message"]["content"])
        self.assertEqual(payload["ainode_pocket"]["device"], self.fakes[0].serial)

    def test_routing_reaches_the_second_device(self):
        model = "Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo"
        status, payload = gateway.chat(self.fleet, {
            "model": model, "max_tokens": 12,
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["ainode_pocket"]["device"], self.fakes[1].serial)

    def test_a_missing_model_is_a_503_that_explains_itself(self):
        status, payload = gateway.chat(self.fleet, {
            "model": "nobody/has-this",
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["type"], "model_not_available")
        self.assertIn("Installed across the fleet", payload["error"]["message"])

    def test_an_unloaded_model_is_a_503_naming_its_device(self):
        status, payload = gateway.chat(self.fleet, {
            "model": "zai-org/GLM-4.7-Flash",
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 503)
        self.assertIn(self.fakes[1].serial, payload["error"]["message"])
        self.assertIn("do not auto-load", payload["error"]["message"])

    def test_bad_requests_are_400(self):
        status, payload = gateway.chat(self.fleet, {"messages": []})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "invalid_request_error")
        status, _ = gateway.chat(self.fleet, {"model": "x"})
        self.assertEqual(status, 400)

    def test_the_ceiling_becomes_a_504_with_advice(self):
        self.fakes[0].state.token_delay = 0.01
        self.fakes[0].state.request_ceiling = 0.05
        status, payload = gateway.chat(self.fleet, {
            "model": "deepreinforce-ai/Ornith-1.0-35B", "max_tokens": 90,
            "messages": [{"role": "user", "content": "write at length"}]})
        self.assertEqual(status, 504)
        self.assertIn("stream=true", payload["error"]["message"])


class TestQueueing(FakeFleetCase):
    """The reason this app exists.

    On the raw device, eight callers arriving together produce collisions: the
    NPU runs one inference at a time and a second request comes back as error
    150004. Through the endpoint they queue and every one of them is served.
    """

    def test_eight_concurrent_callers_all_get_answers(self):
        self.fake.state.token_delay = 0.01
        model = "deepreinforce-ai/Ornith-1.0-35B"
        results = []
        guard = threading.Lock()

        def call():
            status, payload = gateway.chat(self.fleet, {
                "model": model, "max_tokens": 12,
                "messages": [{"role": "user", "content": "hi"}]})
            with guard:
                results.append((status, payload))

        threads = [threading.Thread(target=call) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(results), 8)
        for status, payload in results:
            self.assertEqual(status, 200, payload)
            self.assertTrue(payload["choices"][0]["message"]["content"])
        self.assertEqual(self.fake.state.collisions, 0,
                         "the lock must prevent every 150004")
        self.assertEqual(self.fake.state.served, 8)

    def test_the_device_would_collide_without_the_lock(self):
        """A control: proof the fake really does collide, so the test above is
        measuring the lock and not a device that never had the problem."""
        self.fake.state.token_delay = 0.02
        dev = self.device
        errors = []

        def call():
            try:
                dev.chat({"model": "deepreinforce-ai/Ornith-1.0-35B", "max_tokens": 40,
                          "messages": [{"role": "user", "content": "hi"}]})
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=call) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertTrue(errors)
        self.assertTrue(self.fake.state.collisions)


class TestStreaming(FakeFleetCase):
    def test_a_stream_yields_frames_then_done(self):
        status, error, stream = gateway.chat_stream(self.fleet, {
            "model": "deepreinforce-ai/Ornith-1.0-35B", "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertIsNone(error)
        blob = b"".join(stream).decode()
        self.assertIn("chat.completion.chunk", blob)
        self.assertIn("reasoning_content", blob)
        self.assertTrue(blob.rstrip().endswith("data: [DONE]"))

    def test_a_stream_failure_before_any_bytes_is_a_json_error(self):
        status, error, stream = gateway.chat_stream(self.fleet, {
            "model": "nobody/has-this",
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 503)
        self.assertIsNone(stream)
        self.assertIn("no device in this fleet", error["error"]["message"])

    def test_the_lock_is_released_after_a_stream(self):
        lock = self.fleet.locks[self.fake.serial]
        status, _, stream = gateway.chat_stream(self.fleet, {
            "model": "deepreinforce-ai/Ornith-1.0-35B", "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        list(stream)
        self.assertFalse(lock.busy)
        self.assertEqual(lock.waiting, 0)

    def test_a_stream_holds_the_lock_while_it_runs(self):
        self.fake.state.token_delay = 0.02
        lock = self.fleet.locks[self.fake.serial]
        status, _, stream = gateway.chat_stream(self.fleet, {
            "model": "deepreinforce-ai/Ornith-1.0-35B", "max_tokens": 40,
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        iterator = iter(stream)
        next(iterator)
        self.assertTrue(lock.busy)
        list(iterator)
        self.assertFalse(lock.busy)
