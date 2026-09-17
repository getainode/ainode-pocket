"""The Chat page's own API: the numbers, the model card, and the instances rail.

Everything here runs against the fake device. The numbers a chat reports are the
device's own timings and usage read through the benchmark's derivation, so these
tests care as much about the two agreeing as about the shapes being right.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_server import ServerCase
from pocket import bench as bench_mod
from pocket import server as server_mod

# One completion measured on Jason's Tiiny on 2026-09-16, Qwen/Qwen3-8B through
# POST /v1/chat/completions. Every field below came off the wire; nothing here is
# a rounded-off invention, because the point of this pair is to prove the
# derivation against what the hardware actually sends.
MEASURED_TIMINGS = {"cache_n": 1, "predicted_ms": 439.307, "predicted_n": 10,
                    "predicted_per_second": 22.763124648594264,
                    "predicted_per_token_ms": 43.9307,
                    "prompt_ms": 122.471, "prompt_n": 18,
                    "prompt_per_second": 146.97356925312928,
                    "prompt_per_token_ms": 6.803944444444444}
MEASURED_USAGE = {"completion_tokens": 10, "prompt_tokens": 19,
                  "prompt_tokens_details": {"cached_tokens": 1},
                  "total_tokens": 29}

STAT_KEYS = ("ttft_ms", "prefill_ms", "decode_tok_s", "prefill_tok_s",
             "prompt_tokens", "out_tokens", "cached_tokens", "total_ms",
             "finish_reason", "device", "model")


class TestStatsDerivation(ServerCase):
    """One derivation, two callers.

    The chat bar and a saved benchmark report the same request, so they read the
    same function. Two copies of this arithmetic would drift the first time the
    gateway renamed a field, and then the page and the saved result would
    disagree about a number somebody is about to quote.
    """

    def test_the_captured_pair_derives_the_benchmarks_numbers(self):
        stats = bench_mod.derive_stats(MEASURED_TIMINGS, MEASURED_USAGE)
        self.assertEqual(stats, {
            "prompt_tokens": 19,          # usage wins over timings.prompt_n
            "out_tokens": 10,
            "prefill_tok_s": 146.97,
            "decode_tok_s": 22.76,
            "prefill_ms": 122.5,
            # Prefill plus one token of decode, in seconds. This is the only
            # answer available when the whole reply arrives at once.
            "ttft_s": 0.166,
            "cached_tokens": 1})

    def test_wall_time_is_added_only_when_the_caller_measured_one(self):
        self.assertNotIn("wall_s", bench_mod.derive_stats(MEASURED_TIMINGS,
                                                          MEASURED_USAGE))
        self.assertEqual(bench_mod.derive_stats(MEASURED_TIMINGS, MEASURED_USAGE,
                                                1.23456)["wall_s"], 1.235)

    def test_empty_blocks_derive_zeroes_rather_than_raising(self):
        """A device that answered without a timings block is not a crash."""
        stats = bench_mod.derive_stats(None, None)
        self.assertEqual(stats["out_tokens"], 0)
        self.assertEqual(stats["decode_tok_s"], 0)
        self.assertEqual(stats["cached_tokens"], 0)

    def test_the_chat_stats_object_carries_the_same_numbers_through(self):
        derived = bench_mod.derive_stats(MEASURED_TIMINGS, MEASURED_USAGE)
        stats = server_mod.chat_stats(
            MEASURED_TIMINGS, MEASURED_USAGE, total_ms=812.4, ttft_ms=None,
            finish_reason="stop", device={"id": "TNY1", "name": "tiiny"},
            model="Qwen/Qwen3-8B")
        for field in ("prefill_ms", "decode_tok_s", "prefill_tok_s",
                      "prompt_tokens", "out_tokens", "cached_tokens"):
            self.assertEqual(stats[field], derived[field], field)
        # Nothing streamed, so there was no first token to time here and the
        # device's own answer is used, in milliseconds.
        self.assertEqual(stats["ttft_ms"], 166.0)
        self.assertEqual(stats["total_ms"], 812.4)
        self.assertEqual(stats["device"]["name"], "tiiny")
        for field in STAT_KEYS:
            self.assertIn(field, stats)


class TestChatRoute(ServerCase):
    def stream_chat(self, body):
        """(text, reasoning, stats) from a streamed /api/chat."""
        req = urllib.request.Request(self.base + "/api/chat",
                                     data=json.dumps(body).encode(), method="POST")
        req.add_header("Content-Type", "application/json")
        text, reasoning, stats, done = "", "", None, False
        event = None
        with urllib.request.urlopen(req, timeout=30) as resp:
            self.assertIn("text/event-stream", resp.headers.get("Content-Type"))
            for raw in resp:
                line = raw.decode().strip()
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    done = True
                    break
                frame = json.loads(payload)
                if event == "stats":
                    stats = frame
                    event = None
                    continue
                choices = frame.get("choices") or []
                delta = (choices[0].get("delta") or {}) if choices else {}
                text += delta.get("content") or ""
                reasoning += delta.get("reasoning_content") or ""
        self.assertTrue(done, "the stream ended without [DONE]")
        return text, reasoning, stats

    def test_a_streamed_chat_ends_with_a_stats_event(self):
        text, _, stats = self.stream_chat({
            "model": self.loaded_model(), "stream": True, "max_tokens": 24,
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertTrue(text)
        self.assertIsNotNone(stats, "no event: stats arrived before [DONE]")
        for field in STAT_KEYS:
            self.assertIn(field, stats)
        self.assertEqual(stats["model"], self.loaded_model())
        self.assertEqual(stats["device"]["id"], self.fake.serial)
        self.assertEqual(stats["device"]["name"], self.fake.name)
        self.assertEqual(stats["out_tokens"], 24)
        self.assertEqual(stats["finish_reason"], "length")
        self.assertGreater(stats["decode_tok_s"], 0)
        self.assertGreater(stats["prefill_tok_s"], 0)
        self.assertIsNotNone(stats["ttft_ms"])
        self.assertGreaterEqual(stats["total_ms"], stats["ttft_ms"])

    def test_the_stats_event_comes_before_done_not_after(self):
        """Every SSE client stops reading at [DONE].

        Appending the numbers after that line would put them somewhere no
        ordinary reader ever looks, so they go in front of it.
        """
        req = urllib.request.Request(
            self.base + "/api/chat", method="POST",
            data=json.dumps({"model": self.loaded_model(), "stream": True,
                             "max_tokens": 8,
                             "messages": [{"role": "user", "content": "hi"}]}).encode())
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            blob = resp.read().decode()
        self.assertIn("event: stats", blob)
        self.assertLess(blob.index("event: stats"), blob.index("data: [DONE]"))
        self.assertTrue(blob.rstrip().endswith("data: [DONE]"))
        # Exactly one of each. The device ends its own stream with a [DONE] and
        # the endpoint appends one, and relaying both used to put the numbers in
        # the stream twice, the first time in front of a line that had already
        # told the browser to stop reading.
        self.assertEqual(blob.count("event: stats"), 1)
        self.assertEqual(blob.count("data: [DONE]"), 1)

    def test_a_non_streamed_chat_carries_stats_beside_the_answer(self):
        status, payload = self.request("/api/chat", "POST", {
            "model": self.loaded_model(), "max_tokens": 24,
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertTrue(payload["choices"][0]["message"]["content"])
        stats = payload["stats"]
        for field in STAT_KEYS:
            self.assertIn(field, stats)
        self.assertEqual(stats["device"]["name"], self.fake.name)
        self.assertEqual(stats["out_tokens"], 24)
        self.assertEqual(stats["prompt_tokens"], payload["usage"]["prompt_tokens"])
        self.assertEqual(stats["decode_tok_s"],
                         bench_mod.derive_stats(payload["timings"],
                                                payload["usage"])["decode_tok_s"])

    def test_the_thinking_toggle_is_what_asks_for_reasoning(self):
        """Off means none is requested, not that some is requested and hidden."""
        _, reasoning, _ = self.stream_chat({
            "model": self.loaded_model(), "stream": True, "max_tokens": 40,
            "thinking": False, "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(reasoning, "")
        text, reasoning, stats = self.stream_chat({
            "model": self.loaded_model(), "stream": True, "max_tokens": 40,
            "thinking": True, "messages": [{"role": "user", "content": "hi"}]})
        self.assertTrue(reasoning)
        self.assertTrue(text)
        # The chain of thought is spent out of the same budget the answer needs,
        # so it is counted in the tokens out.
        self.assertEqual(stats["out_tokens"], 40)

    def test_a_system_prompt_is_sent_ahead_of_the_conversation(self):
        status, payload = self.request("/api/chat", "POST", {
            "model": self.loaded_model(), "max_tokens": 16,
            "system": "you are terse",
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        built = server_mod.Handler.chat_request({
            "model": "m", "system": "you are terse",
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(built["messages"][0],
                         {"role": "system", "content": "you are terse"})

    def test_a_system_message_already_there_is_not_doubled(self):
        built = server_mod.Handler.chat_request({
            "model": "m", "system": "second",
            "messages": [{"role": "system", "content": "first"},
                         {"role": "user", "content": "hi"}]})
        self.assertEqual(len(built["messages"]), 2)
        self.assertEqual(built["messages"][0]["content"], "first")

    def test_the_request_asks_for_usage_only_when_it_streams(self):
        streamed = server_mod.Handler.chat_request({
            "model": "m", "stream": True, "thinking": True, "temperature": 0.4,
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(streamed["stream_options"], {"include_usage": True})
        self.assertEqual(streamed["chat_template_kwargs"], {"enable_thinking": True})
        self.assertEqual(streamed["temperature"], 0.4)
        plain = server_mod.Handler.chat_request({
            "model": "m", "messages": [{"role": "user", "content": "hi"}]})
        self.assertNotIn("stream_options", plain)
        self.assertNotIn("stream", plain)
        self.assertEqual(plain["max_tokens"], server_mod.DEFAULT_MAX_TOKENS)

    def test_a_chat_without_a_model_is_a_400(self):
        status, payload = self.request("/api/chat", "POST", {
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 400)
        self.assertIn("model", payload["error"]["message"])

    def test_a_model_that_cannot_chat_is_refused_in_the_usual_words(self):
        status, payload = self.request("/api/chat", "POST", {
            "model": "Qwen/Qwen3-Embedding-0.6B", "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 400)
        self.assertIn("cannot chat", payload["error"]["message"])

    def test_an_unservable_model_is_a_503_with_a_reason(self):
        status, payload = self.request("/api/chat", "POST", {
            "model": "nobody/has-this",
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 503)
        self.assertIn("no device in this fleet", payload["error"]["message"])


class TestModelCard(ServerCase):
    devices = 2

    def card(self, model_id):
        return self.request("/api/model_card?model="
                            + urllib.parse.quote(model_id, safe=""))

    def test_the_card_for_a_loaded_chat_model(self):
        status, card = self.card("deepreinforce-ai/Ornith-1.0-35B")
        self.assertEqual(status, 200)
        self.assertEqual(card["model"], "deepreinforce-ai/Ornith-1.0-35B")
        self.assertEqual(card["name"], "Ornith-1.0-35B")
        self.assertEqual(card["type"], "Image-Text-to-Text")
        self.assertEqual(card["params"], "35B")
        self.assertEqual(card["size_bytes"], 18_000_000_000)
        self.assertEqual(card["npu_usage"], 50)
        self.assertIs(card["can_chat"], True)
        self.assertTrue(card["desc"])
        # The device answers with one word a side, not a list, so the words are
        # wrapped rather than reshaped into something it never said.
        self.assertEqual(card["capabilities"]["output"], ["Text"])
        self.assertTrue(card["capabilities"]["input"])
        loaded = card["loaded_on"]
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["device_id"], self.fakes[0].serial)
        self.assertEqual(loaded[0]["device_name"], self.fakes[0].name)
        self.assertEqual(loaded[0]["status"], "running")

    def test_an_installed_model_that_is_not_loaded_says_so_with_an_empty_list(self):
        status, card = self.card("zai-org/GLM-4.7-Flash")
        self.assertEqual(status, 200)
        self.assertEqual(card["loaded_on"], [])
        self.assertIs(card["can_chat"], True)

    def test_a_model_that_cannot_chat_says_so(self):
        status, card = self.card("Qwen/Qwen3-Embedding-0.6B")
        self.assertEqual(status, 200)
        self.assertIs(card["can_chat"], False)
        self.assertEqual(card["capabilities"]["output"], ["Vector"])

    def test_a_model_nobody_has_is_a_404(self):
        status, payload = self.card("nobody/has-this")
        self.assertEqual(status, 404)
        self.assertIn("no device in this fleet", payload["error"]["message"])

    def test_the_model_is_required(self):
        status, payload = self.request("/api/model_card")
        self.assertEqual(status, 400)
        self.assertIn("model is required", payload["error"]["message"])

    def test_the_catalogue_link_is_derived_and_only_when_it_can_be(self):
        """No device field carries a URL.

        Neither the installed record nor the online catalogue has one: their only
        links are icon paths on the device itself. Model ids here are Hugging
        Face repository paths, so a link can be derived from one, and anything
        that is not shaped like one gets null rather than a guess.
        """
        self.assertEqual(server_mod.catalog_url("Qwen/Qwen3-8B"),
                         "https://huggingface.co/Qwen/Qwen3-8B")
        self.assertIsNone(server_mod.catalog_url("localmodel"))
        self.assertIsNone(server_mod.catalog_url("a/b/c"))
        self.assertIsNone(server_mod.catalog_url(""))
        _, card = self.card("deepreinforce-ai/Ornith-1.0-35B")
        self.assertEqual(card["catalog_url"],
                         "https://huggingface.co/deepreinforce-ai/Ornith-1.0-35B")

    def test_a_record_with_neither_input_nor_output_gets_null(self):
        """Null and an empty list are different claims, so only one is made."""
        self.assertIsNone(server_mod.io_capabilities(None, None))
        self.assertEqual(server_mod.io_capabilities("Text", "Vector"),
                         {"input": ["Text"], "output": ["Vector"]})
        self.assertEqual(server_mod.io_capabilities(["Text", "Image"], None),
                         {"input": ["Text", "Image"], "output": []})


class TestInstances(ServerCase):
    devices = 2

    def test_the_rail_lists_every_loaded_model_on_every_device(self):
        status, payload = self.request("/api/instances")
        self.assertEqual(status, 200)
        for row in payload["instances"]:
            for field in ("device_id", "device_name", "model", "npu_usage",
                          "status", "instance_id"):
                self.assertIn(field, row)
        models = {row["model"] for row in payload["instances"]}
        # The second box has three models loaded and only one of them can chat.
        # The rail shows all three, because they are all spending NPU units.
        self.assertIn("Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice", models)
        self.assertIn("deepreinforce-ai/Ornith-1.0-35B", models)
        self.assertEqual({row["status"] for row in payload["instances"]}, {"running"})

    def test_every_device_reports_its_budget(self):
        status, payload = self.request("/api/instances")
        self.assertEqual(len(payload["devices"]), 2)
        for device in payload["devices"]:
            self.assertIs(device["reachable"], True)
            self.assertEqual(device["npu_total"], 100)
            self.assertGreater(device["npu_used"], 0)
            self.assertIn("device_name", device)
        first = [d for d in payload["devices"] if d["device_id"] == self.fakes[0].serial][0]
        self.assertEqual(first["npu_used"], 50)

    def test_a_device_that_does_not_answer_is_listed_as_unreachable(self):
        """The rail keeps the row rather than dropping the box off the page."""
        self.fakes[1].stop()
        status, payload = self.request("/api/instances")
        self.assertEqual(status, 200)
        record = [d for d in payload["devices"]
                  if d["device_id"] == self.fakes[1].serial][0]
        self.assertIs(record["reachable"], False)
        self.assertIsNone(record["npu_total"])
        self.assertTrue([row for row in payload["instances"]
                         if row["device_id"] == self.fakes[0].serial])


class TestLoadAndUnload(ServerCase):
    def test_a_load_is_accepted_then_comes_up_then_answers(self):
        """Running in npu/status is not proof on its own.

        A load is believable when the status says running and a one token chat
        actually comes back, which is why this test does both.
        """
        model = "Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo"
        status, payload = self.request("/api/instances/load", "POST",
                                       {"device_id": self.fake.serial,
                                        "model": model})
        self.assertEqual(status, 202)
        self.assertIs(payload["ok"], True)
        self.assertEqual(self.status_of(model), "loading")
        status = None
        for _ in range(8):
            status = self.status_of(model)
            if status == "running":
                break
        self.assertEqual(status, "running")
        status, reply = self.request("/api/chat", "POST", {
            "model": model, "max_tokens": 4,
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 200)
        self.assertTrue(reply["choices"][0]["message"]["content"])

    def test_a_model_that_is_still_loading_will_not_answer_yet(self):
        model = "Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo"
        self.request("/api/instances/load", "POST",
                     {"device_id": self.fake.serial, "model": model})
        # Hold it in the loading state for the rest of this test. Every status
        # read advances a pending load by one, and a 35B takes tens of seconds
        # on real hardware rather than two polls.
        self.fake.state.pending[model]["polls"] = 500
        self.assertEqual(self.status_of(model), "loading")
        status, _ = self.request("/api/chat", "POST", {
            "model": model, "max_tokens": 4,
            "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(status, 502, "the device refuses an instance still coming up")

    def test_unload_takes_it_back_off(self):
        model = self.loaded_model()
        status, payload = self.request("/api/instances/unload", "POST",
                                       {"device_id": self.fake.serial, "model": model})
        self.assertEqual(status, 202)
        self.assertIs(payload["ok"], True)
        _, rail = self.request("/api/instances")
        self.assertNotIn(model, [row["model"] for row in rail["instances"]])

    def test_unloading_something_that_is_not_loaded_is_a_400(self):
        status, payload = self.request("/api/instances/unload", "POST",
                                       {"device_id": self.fake.serial,
                                        "model": "zai-org/GLM-4.7-Flash"})
        self.assertEqual(status, 400)
        self.assertIn("is not loaded on", payload["error"]["message"])

    def test_a_model_that_is_not_installed_here_is_refused(self):
        status, payload = self.request("/api/instances/load", "POST",
                                       {"device_id": self.fake.serial,
                                        "model": "openai/gpt-oss-20b"})
        self.assertEqual(status, 400)
        self.assertIn("is not installed on", payload["error"]["message"])
        self.assertIn("Models page", payload["error"]["message"])

    def test_a_model_that_cannot_chat_is_refused(self):
        status, payload = self.request("/api/instances/load", "POST",
                                       {"device_id": self.fake.serial,
                                        "model": "Qwen/Qwen3-Embedding-0.6B"})
        self.assertEqual(status, 400)
        self.assertIn("cannot chat", payload["error"]["message"])
        self.assertNotIn("Qwen/Qwen3-Embedding-0.6B", self.fake.state.pending)

    def test_a_load_that_does_not_fit_the_budget_is_refused_here(self):
        """The device would take it and then roll it back without saying so.

        Measured on real hardware: a start that does not fit answers with the
        same 200 as one that does, the model shows as loading, and then it
        vanishes. Refusing it here is the only place anybody is told.
        """
        # Ornith (50) is already loaded and the Coder Turbo (45) takes the fleet
        # to 95 of 100, so a 30 unit model cannot fit beside them.
        self.fake.state.installed.append("openai/gpt-oss-20b")
        status, _ = self.request("/api/instances/load", "POST",
                                 {"device_id": self.fake.serial,
                                  "model": "Qwen/Qwen3-Coder-30B-A3B-Instruct-Turbo"})
        self.assertEqual(status, 202)
        status, payload = self.request("/api/instances/load", "POST",
                                       {"device_id": self.fake.serial,
                                        "model": "openai/gpt-oss-20b"})
        self.assertEqual(status, 400)
        message = payload["error"]["message"]
        self.assertIn("needs 30 NPU units", message)
        self.assertIn("5 of 100 are free", message)
        self.assertIn("rolls it back", message)
        self.assertNotIn("openai/gpt-oss-20b", self.fake.state.loaded)

    def test_the_models_page_load_shares_the_same_guard(self):
        """Two doors onto dev.start, one set of reasons for refusing.

        The Models page loads speech and embedding models on purpose, so it does
        not ask for a chat model, but it does get the same not installed and
        over budget answers.
        """
        status, payload = self.request("/api/models/load", "POST",
                                       {"device": self.fake.serial,
                                        "model": "openai/gpt-oss-20b"})
        self.assertEqual(status, 400)
        self.assertIn("is not installed on", payload["error"]["message"])
        status, _ = self.request("/api/models/load", "POST",
                                 {"device": self.fake.serial,
                                  "model": "Qwen/Qwen3-Embedding-0.6B"})
        self.assertEqual(status, 200, "an embedding model is a fine thing to load here")

    def test_an_unknown_device_is_a_404(self):
        status, payload = self.request("/api/instances/load", "POST",
                                       {"device_id": "nope", "model": "x"})
        self.assertEqual(status, 404)
        self.assertIn("no device registered", payload["error"]["message"])

    def test_both_fields_are_required(self):
        status, payload = self.request("/api/instances/load", "POST",
                                       {"device_id": self.fake.serial})
        self.assertEqual(status, 400)
        self.assertIn("device_id and model", payload["error"]["message"])

    def status_of(self, model_id):
        """What the rail says about one model right now, or None."""
        _, payload = self.request("/api/instances")
        for row in payload["instances"]:
            if row["model"] == model_id:
                return row["status"]
        return None


class TestChatPage(ServerCase):
    """The page is served, and it asks for nothing off this machine.

    A farm app that pulled a script or a font from a CDN would stop working the
    moment the device is off the internet, which is most of the time.
    """

    def test_every_element_the_chat_view_needs_is_in_the_page(self):
        status, body = self.request("/")
        self.assertEqual(status, 200)
        for element in ("model-card", "instances-list", "load-model", "load-device",
                        "load-btn", "chat-turns", "chat-thinking", "chat-temp",
                        "chat-max-tokens", "chat-system", "conversation-list",
                        "new-chat"):
            self.assertIn('id="%s"' % element, body, element)

    def test_nothing_is_fetched_from_anywhere_but_this_app(self):
        status, body = self.request("/")
        self.assertEqual(status, 200)
        found = re.findall(r'(?:src|href)\s*=\s*"([^"]*)"', body)
        self.assertTrue(found, "the page has no assets at all, which is suspicious")
        for value in found:
            self.assertFalse(value.lower().startswith(("http", "//")),
                             "%s points off this machine" % value)

    def test_the_page_reads_the_servers_own_field_names(self):
        """The bar is only honest if it reads the names the server sends."""
        status, body = self.request("/app.js")
        self.assertEqual(status, 200)
        for field in ("ttft_ms", "decode_tok_s", "total_ms", "prompt_tokens",
                      "out_tokens", "finish_reason", "npu_used", "npu_total",
                      "loaded_on", "can_chat", "catalog_url"):
            self.assertIn(field, body, field)
