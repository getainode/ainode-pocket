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
            "model": "deepreinforce-ai/Ornith-1.0-35B", "max_tokens": 40,
            "chat_template_kwargs": {"enable_thinking": True},
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertIsNone(error)
        blob = b"".join(stream).decode()
        self.assertIn("chat.completion.chunk", blob)
        # Reasoning comes back because this request asked for it. Nothing in the
        # endpoint adds or strips the flag: the device's frames go through as
        # they arrive.
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


class TestModelKinds(FakeFleetCase):
    """A Tiiny holds speech, embedding and image models next to the chat ones.

    The device answers a chat completion aimed at one of them with its own
    "does not support chat" error, which reads as a broken app rather than a
    wrong choice, so the endpoint refuses first and says what the model is.
    """

    devices = 2

    def test_the_device_tells_us_what_each_model_is(self):
        rows = {row["model_id"]: row for row in self.fleet.models(self.fakes[1].serial)}
        tts = rows["Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"]
        self.assertEqual(tts["type"], "Text-to-Speech")
        self.assertEqual(tts["capabilities"], ["voice"])
        self.assertFalse(tts["chat"])
        self.assertTrue(tts["loaded"], "the fixture has it loaded, as a real box does")
        coder = rows["Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo"]
        self.assertEqual(coder["capabilities"], ["main"])
        self.assertTrue(coder["chat"])

    def test_a_capabilityless_row_falls_back_to_its_type(self):
        """Older catalogue rows carry a type and no capabilities at all."""
        self.fakes[0].state.installed.append("PaddlePaddle/PP-OCRv6-Small")
        self.fleet.invalidate()
        rows = {row["model_id"]: row for row in self.fleet.models(self.fakes[0].serial,
                                                                 force=True)}
        ocr = rows["PaddlePaddle/PP-OCRv6-Small"]
        self.assertEqual(ocr["capabilities"], [])
        self.assertFalse(ocr["chat"])

    def test_every_model_is_still_listed_with_a_chat_flag(self):
        """/v1/models stays the full list. That is the OpenAI contract, and a
        client that filters on nothing must still see everything installed."""
        rows = {row["id"]: row for row in gateway.models_payload(self.fleet)["data"]}
        self.assertIn("Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", rows)
        self.assertIn("Qwen/Qwen3-Embedding-0.6B", rows)
        self.assertFalse(rows["Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"]
                         ["ainode_pocket"]["chat"])
        self.assertFalse(rows["Qwen/Qwen3-Embedding-0.6B"]["ainode_pocket"]["chat"])
        self.assertTrue(rows["deepreinforce-ai/Ornith-1.0-35B"]["ainode_pocket"]["chat"])
        self.assertTrue(rows["Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo"]
                        ["ainode_pocket"]["chat"])
        # A vision model is a chat model: it answers /v1/chat/completions.
        self.assertEqual(rows["deepreinforce-ai/Ornith-1.0-35B"]
                         ["ainode_pocket"]["capabilities"], ["main"])

    def test_chat_with_a_speech_model_is_a_400_that_names_the_alternatives(self):
        status, payload = gateway.chat(self.fleet, {
            "model": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", "max_tokens": 24,
            "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "invalid_request_error")
        message = payload["error"]["message"]
        self.assertIn("Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice is a text-to-speech "
                      "model and cannot chat.", message)
        self.assertIn("Loaded chat models:", message)
        self.assertIn("Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo", message)
        self.assertIn("deepreinforce-ai/Ornith-1.0-35B", message)
        self.assertNotIn("Qwen/Qwen3-Embedding-0.6B", message.split("Loaded chat models:")[1])

    def test_the_refusal_never_reaches_the_device(self):
        """A 400 the endpoint decides itself, not a device error dressed up."""
        before = self.fakes[1].state.served
        status, _ = gateway.chat(self.fleet, {
            "model": "Qwen/Qwen3-Embedding-0.6B", "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 400)
        self.assertEqual(self.fakes[1].state.served, before,
                         "nothing was asked of the device")

    def test_an_embedding_model_is_refused_by_what_it_is(self):
        status, payload = gateway.chat(self.fleet, {
            "model": "Qwen/Qwen3-Embedding-0.6B",
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 400)
        self.assertIn("is an embedding model and cannot chat",
                      payload["error"]["message"])

    def test_an_installed_but_unloaded_non_chat_model_is_still_a_400(self):
        """Type beats load state: loading it would not make it able to chat."""
        status, payload = gateway.chat(self.fleet, {
            "model": "Qwen/Qwen3-ASR-1.7B",
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 400)
        self.assertIn("is a speech recognition model and cannot chat",
                      payload["error"]["message"])

    def test_a_streaming_request_is_refused_the_same_way(self):
        status, payload, stream = gateway.chat_stream(self.fleet, {
            "model": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", "stream": True,
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 400)
        self.assertIsNone(stream, "refused before a single byte goes out")
        self.assertIn("cannot chat", payload["error"]["message"])

    def test_an_unknown_model_is_still_the_router_503(self):
        """Not a 400: Pocket cannot say what a model it has never seen is."""
        status, payload = gateway.chat(self.fleet, {
            "model": "nobody/has-this",
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 503)

    def test_a_chat_model_still_answers(self):
        status, payload = gateway.chat(self.fleet, {
            "model": "deepreinforce-ai/Ornith-1.0-35B", "max_tokens": 16,
            "messages": [{"role": "user", "content": "hello"}]})
        self.assertEqual(status, 200)
        self.assertTrue(payload["choices"][0]["message"]["content"])


class TestNothingLoadedToChatWith(FakeFleetCase):
    """One box with only an image model loaded: the third fixture spread."""

    devices = 3

    def setUp(self):
        super().setUp()
        for fake in self.fakes[:2]:
            fake.state.loaded = []
        self.fleet.invalidate()

    def test_the_refusal_says_so_instead_of_listing_nothing(self):
        status, payload = gateway.chat(self.fleet, {
            "model": "Tongyi-MAI/Z-Image-Turbo",
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 400)
        message = payload["error"]["message"]
        self.assertIn("is an image generation model and cannot chat", message)
        self.assertIn("No chat model is loaded right now", message)
        self.assertIn("Models page", message)

    def test_the_fleet_reports_no_loaded_chat_model(self):
        self.assertEqual(self.fleet.chat_models(), [])
        self.assertTrue(self.fleet.chat_models(loaded_only=False),
                        "installed chat models are still known")
