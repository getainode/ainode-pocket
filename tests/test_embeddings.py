"""Offline coverage for the OpenAI embeddings endpoint."""
import http.client
import json
import os
import socket
import subprocess
import sys
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_server import ServerCase
from pocket import device as device_mod
from pocket import gateway, server

MODEL = "Qwen/Qwen3-Embedding-0.6B"


class TestEmbeddings(ServerCase):
    def setUp(self):
        super().setUp()
        # Change only the offline fixture, never load a hardware model.
        self.fake.state.loaded.append(MODEL)
        self.fleet.invalidate()

    def post(self, body, headers=None):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        connection = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=10)
        try:
            connection.request("POST", "/v1/embeddings", raw,
                               headers={"Content-Type": "application/json", **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read().decode())
        finally:
            connection.close()

    def test_string_and_list_inputs_route_to_the_loaded_model(self):
        status, payload = self.post({"model": MODEL, "input": "hello"})
        self.assertEqual(status, 200, payload)
        self.assertEqual((payload["object"], len(payload["data"]), payload["model"]), ("list", 1, MODEL))
        self.assertEqual(payload["ainode_pocket"]["device"], self.fake.serial)
        status, payload = self.post({"model": MODEL, "input": ["a", "b", "c"]})
        self.assertEqual([row["index"] for row in payload["data"]], [0, 1, 2])
        self.assertNotEqual(payload["data"][0]["embedding"], payload["data"][1]["embedding"])

    def test_bad_inputs_are_refused_before_any_device_call(self):
        with mock.patch.object(device_mod.Device, "embeddings") as call:
            for body in ({"input": "x"}, {"model": MODEL}, {"model": MODEL, "input": ""},
                         {"model": MODEL, "input": []}, {"model": MODEL, "input": [1, 2]},
                         {"model": MODEL, "input": ["ok", None]}, {"model": ["x"], "input": "x"}):
                status, payload = self.post(body)
                self.assertEqual(status, 400, body)
            status, _ = self.post({"model": MODEL, "input": ["x"] * (gateway.MAX_EMBED_INPUTS + 1)})
            self.assertEqual(status, 413)
            status, _ = self.post({"model": MODEL, "input": "x" * (gateway.MAX_EMBED_CHARS + 1)})
            self.assertEqual(status, 413)
            self.assertFalse(call.called)

    def test_chat_model_is_refused_with_its_type(self):
        status, payload = self.post({"model": "deepreinforce-ai/Ornith-1.0-35B", "input": "x"})
        self.assertEqual(status, 400)
        self.assertIn("cannot embed text", payload["error"]["message"])

    def test_oversized_body_is_refused_before_reading(self):
        status, payload = self.post(b"", headers={"Content-Length": str(server.MAX_EMBED_BODY + 1)})
        self.assertEqual(status, 413, payload)

    def test_model_not_loaded_anywhere_is_503(self):
        self.fake.state.loaded.remove(MODEL)
        self.fleet.invalidate()
        status, payload = self.post({"model": MODEL, "input": "x"})
        self.assertEqual(status, 503, payload)

    def test_busy_device_is_retried_then_reported(self):
        with mock.patch.object(device_mod.Device, "embeddings", side_effect=device_mod.DeviceBusy("busy", code=150004)), \
                mock.patch.object(gateway, "BUSY_BACKOFF", 0), mock.patch.object(gateway.time, "sleep"):
            status, payload = self.post({"model": MODEL, "input": "x"})
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["type"], "device_busy")


class TestEmbeddingsFraming(ServerCase):
    """Daybreak DB-EMB-001..003: strict framing, bounded queueing, no device detail in replies."""
    def setUp(self):
        super().setUp()
        self.fake.state.loaded.append(MODEL)
        self.fleet.invalidate()

    def raw(self, head, body=b"", half_close=False):
        """Send raw bytes; return the HTTP status lines written and whether the server closed within 2 s."""
        # Half-close only when asked: a half-closed client makes the server stop early and hides keep-alive.
        with socket.create_connection(("127.0.0.1", self.httpd.server_port), timeout=2) as sock:
            sock.sendall(head.encode() + body)
            if half_close:
                sock.shutdown(socket.SHUT_WR)
            data, closed = b"", False
            try:
                while not closed:
                    chunk = sock.recv(65536)
                    closed = not chunk
                    data += chunk
            except socket.timeout:
                pass
        return [line for line in data.decode("latin-1").split("\r\n") if line.startswith("HTTP/1.")], closed

    def test_bad_framing_is_refused_and_the_connection_closed(self):
        # A clean request right behind the bad headers: kept alive, the server would answer it too (desync).
        second = b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n"
        for headers in ("Content-Length: -1\r\n", "", "Content-Length: 0\r\nContent-Length: 0\r\n",
                        "Transfer-Encoding: chunked\r\n", "Transfer-Encoding: chunked\r\nContent-Length: 0\r\n"):
            statuses, closed = self.raw("POST /v1/embeddings HTTP/1.1\r\nHost: x\r\n%s\r\n" % headers, second)
            self.assertTrue(closed, headers)
            self.assertEqual(len(statuses), 1, (headers, statuses))
            self.assertIn(" 400 ", statuses[0], headers)

    def test_only_plain_decimal_lengths_are_accepted(self):
        body = json.dumps({"model": MODEL, "input": "hello there"}).encode()
        n = str(len(body))
        for value in ("+" + n, n[0] + "_" + n[1:], "abc", "-" + n):
            statuses, closed = self.raw("POST /v1/embeddings HTTP/1.1\r\nHost: x\r\nContent-Length: %s\r\n\r\n" % value, body)
            self.assertTrue(closed, value)
            self.assertEqual(len(statuses), 1, (value, statuses))
            self.assertIn(" 400 ", statuses[0], value)
        statuses, _ = self.raw("POST /v1/embeddings HTTP/1.1\r\nHost: x\r\nContent-Length: %s\r\n\r\n" % n, body)
        self.assertIn(" 200 ", statuses[0])

    def test_short_body_is_refused(self):
        statuses, _ = self.raw("POST /v1/embeddings HTTP/1.1\r\nHost: x\r\nContent-Length: 500\r\n\r\n", b"{}", half_close=True)
        self.assertEqual(len(statuses), 1)
        self.assertIn(" 400 ", statuses[0])

    def test_full_slots_answer_503(self):
        held = [server.EMBED_SLOTS.acquire(blocking=False) for _ in range(4)]
        try:
            self.assertTrue(all(held))
            status, payload = TestEmbeddings.post(self, {"model": MODEL, "input": "x"})
            self.assertEqual((status, payload["error"]["type"]), (503, "server_busy"))
        finally:
            for _ in held:
                server.EMBED_SLOTS.release()

    def test_lock_wait_is_bounded(self):
        with mock.patch.object(gateway, "EMBED_LOCK_WAIT", 0.2):
            dev, lock = self.fleet.route(MODEL)
            held = lock.acquire(why="test holds the device")
            try:
                started = time.monotonic()
                status, payload = TestEmbeddings.post(self, {"model": MODEL, "input": "x"})
                waited = time.monotonic() - started
            finally:
                lock.release(held)
        self.assertEqual((status, payload["error"]["type"]), (503, "device_busy"))
        self.assertLess(waited, 2, "a queued request gives up at EMBED_LOCK_WAIT, not the 600 s default")

    def test_another_process_holding_the_lock_file_is_bounded(self):
        """DB-EMB-R2-001: the deadline covers the cross-process flock, not only the in-process lock."""
        dev, lock = self.fleet.route(MODEL)
        holder = subprocess.Popen(
            [sys.executable, "-I", "-c", "import fcntl,os,sys,time; fd=os.open(sys.argv[1], os.O_RDWR|os.O_CREAT); "
             "fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); time.sleep(30)", lock.path],
            stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            with mock.patch.object(gateway, "EMBED_LOCK_WAIT", 0.3):
                started = time.monotonic()
                status, payload = TestEmbeddings.post(self, {"model": MODEL, "input": "x"})
                waited = time.monotonic() - started
            self.assertEqual((status, payload["error"]["type"]), (503, "device_busy"))
            self.assertLess(waited, 2, "the external file lock wait must end at EMBED_LOCK_WAIT")
            self.assertFalse(lock.busy, "a timed-out acquire must release what it took")
        finally:
            holder.kill()
            holder.wait()
            holder.stdout.close()
        status, _ = TestEmbeddings.post(self, {"model": MODEL, "input": "x"})
        self.assertEqual(status, 200, "the lock and the request slot recover once the holder exits")

    def test_unnamed_device_address_is_not_in_errors(self):
        """DB-EMB-R2-002: a device without a friendly name is named by its address; errors name no device."""
        dev, _ = self.fleet.route(MODEL)
        busy = device_mod.DeviceBusy("busy", code=150004)
        for exc in (device_mod.DeviceError("x"), device_mod.DeviceTimeout("x"), busy):
            with mock.patch.object(dev, "name", "10.9.8.7"), mock.patch.object(dev, "id", "10.9.8.7"), \
                    mock.patch.object(device_mod.Device, "embeddings", side_effect=exc), \
                    mock.patch.object(gateway, "BUSY_BACKOFF", 0), mock.patch.object(gateway.time, "sleep"), \
                    mock.patch("sys.stderr"):
                status, payload = TestEmbeddings.post(self, {"model": MODEL, "input": "x"})
            self.assertIn(status, (502, 503, 504))
            self.assertNotIn("10.9.8.7", json.dumps(payload), type(exc).__name__)

    def test_device_errors_never_carry_device_detail(self):
        secret = "http://10.9.8.7:8080/v1/embeddings returned 500: internal stack"
        for exc in (device_mod.DeviceError(secret), device_mod.DeviceTimeout(secret)):
            with mock.patch.object(device_mod.Device, "embeddings", side_effect=exc), \
                    mock.patch("sys.stderr"):
                status, payload = TestEmbeddings.post(self, {"model": MODEL, "input": "x"})
            self.assertIn(status, (502, 504))
            self.assertNotIn("10.9.8.7", json.dumps(payload))
            self.assertNotIn("stack", json.dumps(payload))
