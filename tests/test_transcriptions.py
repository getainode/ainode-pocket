"""Offline coverage for the OpenAI audio transcription endpoint."""
import http.client
import json
import os
import sys
import threading
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_server import ServerCase
from pocket import device as device_mod
from pocket import gateway, multipart, server

MODEL = "Qwen/Qwen3-ASR-1.7B"


def upload(**overrides):
    fields = {"model": MODEL, "file": ("a.wav", b"RIFF\x00\xffaudio", "audio/wav")}
    fields.update(overrides)
    return fields


class TestTranscriptions(ServerCase):
    def setUp(self):
        super().setUp()
        # Change only the offline fixture, never load a hardware model.
        self.fake.state.loaded.append(MODEL)
        self.fleet.invalidate()

    def post(self, fields=None, raw=None, headers=None):
        if raw is None:
            raw, content_type = multipart.encode(upload() if fields is None else fields)
        else:
            content_type = "multipart/form-data; boundary=manual"
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=10)
        try:
            connection.request("POST", "/v1/audio/transcriptions", raw,
                               headers={"Content-Type": content_type, **(headers or {})})
            response = connection.getresponse()
            data = response.read().decode()
            return (response.status, response.getheader("Content-Type"),
                    json.loads(data) if response.getheader("Content-Type") == "application/json" else data)
        finally:
            connection.close()

    def test_json_through_fake_is_unmodified(self):
        status, content_type, payload = self.post()
        self.assertEqual((status, content_type), (200, "application/json"))
        self.assertEqual(payload, {"text": "This is a fake transcription."})
        self.assertEqual(self.fake.state.last_transcription["file"], upload()["file"])

    def test_text_through_fake(self):
        status, content_type, payload = self.post(upload(response_format="text"))
        self.assertEqual((status, content_type, payload),
                         (200, "text/plain; charset=utf-8", "This is a fake transcription."))

    def test_oversized_upload_is_rejected_before_reading(self):
        status, _, payload = self.post(raw=b"", headers={"Content-Length": str(server.MAX_AUDIO_UPLOAD + 1)})
        self.assertEqual(status, 413)
        self.assertEqual(payload["error"]["message"], "Audio uploads must be 25 MiB or smaller.")
        self.assertEqual(self.fake.state.served, 0)

    def test_missing_and_invalid_lengths(self):
        for length in (None, "-1", "invalid"):
            with self.subTest(length=length):
                connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=10)
                try:
                    connection.putrequest("POST", "/v1/audio/transcriptions")
                    if length is not None:
                        connection.putheader("Content-Length", length)
                    connection.endheaders()
                    response = connection.getresponse()
                    self.assertEqual(response.status, 400)
                    self.assertEqual(json.loads(response.read())["error"]["type"], "invalid_request_error")
                finally:
                    connection.close()

    def test_missing_file(self):
        status, _, payload = self.post({"model": MODEL})
        self.assertEqual(status, 400)
        self.assertIn("file", payload["error"]["message"])

    def test_empty_file(self):
        status, _, _ = self.post(upload(file=("empty.wav", b"", "audio/wav")))
        self.assertEqual(status, 400)

    def test_missing_model(self):
        status, _, payload = self.post({"file": upload()["file"]})
        self.assertEqual(status, 400)
        self.assertIn("model", payload["error"]["message"])

    def test_bad_response_format(self):
        status, _, payload = self.post(upload(response_format="verbose_json"))
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "invalid_request_error")

    def test_non_asr_model_names_type(self):
        status, _, payload = self.post(upload(model=self.loaded_model()))
        self.assertEqual(status, 400)
        self.assertIn("Image-Text-to-Text", payload["error"]["message"])
        self.assertEqual(self.fake.state.served, 0)

    def test_no_unit_holds_model(self):
        status, _, payload = self.post(upload(model="nobody/asr"))
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["type"], "model_not_available")

    def test_model_is_installed_but_unloaded(self):
        self.fake.state.loaded.remove(MODEL)
        self.fleet.invalidate()
        status, _, payload = self.post()
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["type"], "model_not_available")

    def test_quote_filename_and_unicode_prompt_survive_both_hops(self):
        # Handwritten input exercises parsing independently of Pocket's encoder.
        raw = (b'--manual\r\nContent-Disposition: form-data; name="file"; '
               b'filename="say \\"hello\\".wav"\r\nContent-Type: audio/wav\r\n\r\naudio\r\n'
               b'--manual\r\nContent-Disposition: form-data; name="model"\r\n\r\n' + MODEL.encode() +
               b'\r\n--manual\r\nContent-Disposition: form-data; name="prompt"\r\n\r\n' +
               'Héllo 世界'.encode() + b'\r\n--manual--\r\n')
        status, _, _ = self.post(raw=raw)
        self.assertEqual(status, 200)
        fields = self.fake.state.last_transcription
        self.assertEqual(fields["file"][0], 'say "hello".wav')
        self.assertEqual(fields["prompt"], "Héllo 世界")

    def test_optional_fields_are_forwarded(self):
        status, _, _ = self.post(upload(prompt="héllo", language="fr", temperature="0.2"))
        self.assertEqual(status, 200)
        fields = self.fake.state.last_transcription
        self.assertEqual((fields["prompt"], fields["language"], fields["temperature"]),
                         ("héllo", "fr", "0.2"))

    def test_malformed_multipart(self):
        for raw in (b"garbage", b'--manual\r\nContent-Disposition: form-data; name="model"\r\n\r\nx'):
            with self.subTest(raw=raw):
                status, _, payload = self.post(raw=raw)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["type"], "invalid_request_error")

    def test_models_metadata_keys_stay_unchanged(self):
        status, payload = self.request("/v1/models")
        self.assertEqual(status, 200)
        for row in payload["data"]:
            self.assertEqual(set(row["ainode_pocket"]),
                             {"type", "chat", "ready", "loaded_on", "devices",
                              "capabilities", "params", "size"})
        asr = next(row for row in payload["data"] if row["id"] == MODEL)
        self.assertEqual(asr["ainode_pocket"]["type"], "ASR")

    def test_concurrent_call_waits_while_device_lock_is_held(self):
        entered = threading.Event()
        release = threading.Event()
        original = self.device.transcribe
        lock = self.fleet.locks[self.fake.serial]
        calls = []
        results = []

        def transcribe(fields, timeout=600):
            calls.append(lock.busy)
            entered.set()
            if not release.wait(5):
                raise AssertionError("test did not release transcription")
            return original(fields, timeout)

        def wait_for(predicate):
            deadline = time.monotonic() + 5
            while not predicate() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(predicate())

        with mock.patch.object(self.device, "transcribe", side_effect=transcribe):
            first = threading.Thread(target=lambda: results.append(self.post()))
            second = threading.Thread(target=lambda: results.append(self.post()))
            try:
                first.start()
                self.assertTrue(entered.wait(5))
                second.start()
                wait_for(lambda: lock.waiting == 1)
                self.assertEqual(calls, [True])
            finally:
                release.set()
                first.join(10)
                if second.ident is not None:
                    second.join(10)
        self.assertEqual([row[0] for row in results], [200, 200])
        self.assertEqual(calls, [True, True])
        self.assertFalse(lock.busy)
        self.assertEqual(self.fake.state.collisions, 0)

    def test_busy_retries_and_invalidates_after_call(self):
        with mock.patch.object(self.device, "transcribe", side_effect=[
                device_mod.DeviceBusy("busy"), {"text": "done"}]) as call, \
                mock.patch.object(gateway.time, "sleep"), \
                mock.patch.object(self.fleet, "invalidate", wraps=self.fleet.invalidate) as invalidate:
            status, payload = gateway.transcribe(self.fleet, upload())
        self.assertEqual((status, payload), (200, {"text": "done"}))
        self.assertEqual(call.call_count, 2)
        invalidate.assert_called_once_with(self.fake.serial)

    def test_device_failures_use_error_envelope_and_release_lock(self):
        for exc, expected, kind in ((device_mod.DeviceTimeout("timeout"), 504, "gateway_timeout"),
                                    (device_mod.DeviceError("failed"), 502, "device_error"),
                                    (device_mod.DeviceBusy("busy"), 503, "device_busy")):
            with self.subTest(kind=kind), \
                    mock.patch.object(self.device, "transcribe", side_effect=exc), \
                    mock.patch.object(gateway.time, "sleep"):
                status, payload = gateway.transcribe(self.fleet, upload())
                self.assertEqual((status, payload["error"]["type"]), (expected, kind))
                self.assertFalse(self.fleet.locks[self.fake.serial].busy)

    def test_text_response_still_recognizes_in_band_busy(self):
        self.fake.state.inference.acquire()
        try:
            with self.assertRaises(device_mod.DeviceBusy):
                self.device.transcribe(upload(response_format="text"))
        finally:
            self.fake.state.inference.release()


    def stalled_upload(self, sent=1, declared=100):
        import socket
        sock = socket.create_connection(("127.0.0.1", self.httpd.server_port), timeout=10)
        sock.sendall(("POST /v1/audio/transcriptions HTTP/1.1\r\nHost: x\r\n"
                      "Content-Type: multipart/form-data; boundary=b\r\n"
                      "Content-Length: %d\r\n\r\n" % declared).encode() + b"x" * sent)
        return sock

    def test_stalled_upload_times_out_and_frees_its_slot(self):
        # DB-ASR-001: a client that stops sending gets 408 instead of holding a thread forever.
        with mock.patch.object(server, "UPLOAD_IDLE_SECONDS", 0.3):
            sock = self.stalled_upload()
            try:
                reply = sock.recv(200).decode(errors="replace")
            finally:
                sock.close()
        self.assertIn(" 408 ", reply.split("\r\n")[0])
        status, _, _ = self.post()
        self.assertEqual(status, 200)

    def test_total_deadline_applies_to_a_trickle(self):
        with mock.patch.object(server, "UPLOAD_TOTAL_SECONDS", 0.5):
            sock = self.stalled_upload()
            try:
                started = time.monotonic()
                reply = sock.recv(200).decode(errors="replace")
            finally:
                sock.close()
        self.assertIn(" 408 ", reply.split("\r\n")[0])
        self.assertLess(time.monotonic() - started, 5)

    def test_uploads_beyond_the_slot_limit_get_503(self):
        slots = threading.BoundedSemaphore(1)
        with mock.patch.object(server, "UPLOAD_SLOTS", slots), \
                mock.patch.object(server, "UPLOAD_IDLE_SECONDS", 5):
            sock = self.stalled_upload()
            try:
                for _ in range(50):
                    if not slots.acquire(blocking=False):
                        break
                    slots.release()
                    time.sleep(0.02)
                status, _, payload = self.post()
            finally:
                sock.close()
            for _ in range(250):  # leave the patch only after the stalled handler gives its slot back
                if slots.acquire(blocking=False):
                    slots.release()
                    break
                time.sleep(0.02)
        self.assertEqual(status, 503)
        self.assertIn("Too many audio uploads", payload["error"]["message"])


    def test_upload_slot_is_held_through_the_device_call(self):
        # DB-ASR-001 round 2: the permit covers parse, device wait and reply, not just the read.
        slots = threading.BoundedSemaphore(1)
        seen = []
        real = gateway.transcribe

        def spy(fleet, fields, **kw):
            seen.append(slots.acquire(blocking=False))
            return real(fleet, fields, **kw)

        with mock.patch.object(server, "UPLOAD_SLOTS", slots), \
                mock.patch.object(gateway, "transcribe", spy):
            status, _, _ = self.post()
        self.assertEqual((status, seen), (200, [False]))
        self.assertTrue(slots.acquire(blocking=False))

    def test_connection_that_sends_no_headers_is_closed(self):
        # DB-ASR-002: the header phase has its own deadline before any route runs.
        import socket
        with mock.patch.object(server, "REQUEST_IDLE_SECONDS", 0.3):
            sock = socket.create_connection(("127.0.0.1", self.httpd.server_port), timeout=10)
            try:
                started = time.monotonic()
                data = sock.recv(100)
            finally:
                sock.close()
        self.assertLess(time.monotonic() - started, 5)
        self.assertNotIn(b"200 OK", data)

    def test_connections_past_the_cap_are_closed_on_accept(self):
        import socket
        self.httpd.connections = threading.BoundedSemaphore(1)
        first = socket.create_connection(("127.0.0.1", self.httpd.server_port), timeout=10)
        try:
            for _ in range(50):
                if not self.httpd.connections.acquire(blocking=False):
                    break
                self.httpd.connections.release()
                time.sleep(0.02)
            second = socket.create_connection(("127.0.0.1", self.httpd.server_port), timeout=10)
            try:
                second.sendall(b"GET /api/state HTTP/1.1\r\nHost: x\r\n\r\n")
                self.assertEqual(second.recv(100), b"")
            except ConnectionResetError:
                pass
            finally:
                second.close()
        finally:
            first.close()


    def test_trickled_headers_hit_the_absolute_deadline(self):
        # DB-ASR-002 round 3: a byte every 0.1 s never trips the idle timeout, the header deadline ends it.
        import socket
        with mock.patch.object(server, "HEADER_SECONDS", 0.6), \
                mock.patch.object(server, "REQUEST_IDLE_SECONDS", 5):
            sock = socket.create_connection(("127.0.0.1", self.httpd.server_port), timeout=10)
            started = time.monotonic()
            closed = False
            try:
                for ch in b"GET /api/state HTTP/1.1\r\nX-Slow: " + b"a" * 200:
                    sock.sendall(bytes([ch]))
                    time.sleep(0.1)
                    if time.monotonic() - started > 4:
                        break
            except (BrokenPipeError, ConnectionResetError):
                closed = True
            finally:
                sock.close()
        self.assertTrue(closed)
        self.assertLess(time.monotonic() - started, 4)
        status, _, _ = self.post()
        self.assertEqual(status, 200)


class TestTranscriptionVhost(TestTranscriptions):
    fake_kwargs = {"vhost_only": True}

    def test_uses_openai_virtual_host(self):
        self.assertEqual(self.post()[0], 200)
        self.assertEqual(self.device.transport, device_mod.TRANSPORT_VHOST)
        self.assertEqual(self.device.vhost_for("/v1/audio/transcriptions"), device_mod.OPENAI_VHOST)
