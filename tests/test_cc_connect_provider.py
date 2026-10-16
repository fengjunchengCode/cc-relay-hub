import http.server
import json
import os
import socketserver
import sys
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests import require_sandbox
require_sandbox()

from core.envelope import RelayEnvelope  # noqa: E402
from providers.cc_connect import CCConnectProvider, _parse_timestamp  # noqa: E402


class CaptureHandler(http.server.BaseHTTPRequestHandler):
    payloads = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers["Content-Length"])
        body = self.rfile.read(length)
        CaptureHandler.payloads.append(json.loads(body.decode("utf-8")))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{\"ok\":true}")

    def log_message(self, fmt, *args):  # noqa: A003
        return


class CCConnectProviderTest(unittest.TestCase):
    def test_parse_timestamp_accepts_variable_precision_offset(self):
        parsed = _parse_timestamp("2026-05-05T20:47:34.03814+08:00")
        self.assertGreater(parsed, 0.0)

    def test_session_file_picks_most_recent_when_multiple_exist(self):
        # An agent accumulates several session files as it is re-initialized over
        # time. glob order is arbitrary, so the provider must pick the active
        # (most recently modified) one — otherwise the hub polls a stale, dead
        # session file and misses replies entirely (--wait times out even though
        # the agent answered correctly).
        with tempfile.TemporaryDirectory() as home:
            sessions = Path(home) / ".cc-connect" / "sessions"
            sessions.mkdir(parents=True)
            stale = sessions / "codex-bot_aaaaaaaa.json"
            active = sessions / "codex-bot_zzzzzzzz.json"
            stale.write_text("{}", encoding="utf-8")
            active.write_text("{}", encoding="utf-8")
            os.utime(stale, (1000.0, 1000.0))
            os.utime(active, (2000.0, 2000.0))

            old_home = os.environ.get("HOME")
            os.environ["HOME"] = home
            try:
                provider = CCConnectProvider("codex-bot", {"session_key": "feishu:s:u"})
                resolved = provider._session_file()
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home

            self.assertEqual(resolved, active)

    def test_session_activity_uses_last_history_role_not_recency_guess(self):
        with tempfile.TemporaryDirectory() as data_dir:
            sessions = Path(data_dir) / "sessions"
            sessions.mkdir()
            session_path = sessions / "codex-bot_active.json"
            session_path.write_text(
                json.dumps({
                    "sessions": {
                        "s1": {
                            "history": [{
                                "role": "user",
                                "content": "long running turn",
                                "timestamp": "2026-07-25T10:00:00-04:00",
                            }]
                        }
                    }
                }),
                encoding="utf-8",
            )
            provider = CCConnectProvider(
                "codex-bot",
                {
                    "session_key": "feishu:s1:u1",
                    "data_dir": data_dir,
                },
            )

            busy = provider.get_session_activity()
            self.assertEqual(busy["status"], "busy")
            self.assertIsNone(busy["idle_token"])

            data = json.loads(session_path.read_text(encoding="utf-8"))
            data["sessions"]["s1"]["history"].append({
                "role": "assistant",
                "content": "final",
                "timestamp": "2026-07-25T11:30:00-04:00",
            })
            session_path.write_text(json.dumps(data), encoding="utf-8")

            idle = provider.get_session_activity()
            self.assertEqual(idle["status"], "idle")
            self.assertIn("2026-07-25T11:30:00-04:00", idle["idle_token"])

    def test_session_activity_returns_unknown_for_partial_history_file(self):
        with tempfile.TemporaryDirectory() as data_dir:
            sessions = Path(data_dir) / "sessions"
            sessions.mkdir()
            (sessions / "codex-bot_active.json").write_text(
                "{partial",
                encoding="utf-8",
            )
            provider = CCConnectProvider(
                "codex-bot",
                {
                    "session_key": "feishu:s1:u1",
                    "data_dir": data_dir,
                },
            )

            activity = provider.get_session_activity()
            self.assertEqual(activity["status"], "unknown")
            self.assertIsNone(activity["idle_token"])

    def test_deliver_posts_webhook_payload(self):
        with tempfile.TemporaryDirectory():
            CaptureHandler.payloads = []
            server = socketserver.TCPServer(("127.0.0.1", 0), CaptureHandler)
            thread = threading.Thread(target=server.serve_forever)
            thread.daemon = True
            thread.start()
            try:
                port = server.server_address[1]
                provider = CCConnectProvider(
                    agent_id="codex-bot",
                    binding={
                        "webhook_port": port,
                        "webhook_path": "/hook",
                        "session_key": "feishu:s1:u1",
                        "delivery_ack_timeout": 0,
                    },
                )
                envelope = RelayEnvelope(
                    request_id="req-1",
                    sender="hub",
                    target="codex-bot",
                    body="ping",
                    created_at=1.0,
                    reply_to=None,
                    ttl=30,
                )

                receipt = provider.deliver(envelope)

                self.assertEqual(receipt.status, "delivered")
                payload = CaptureHandler.payloads[-1]
                self.assertEqual(payload["session_key"], "feishu:s1:u1")
                self.assertEqual(payload["platform"], "feishu")
                self.assertEqual(payload["event"], "cc-relay")
                self.assertIn("ping", payload["prompt"])
                self.assertIn("[cc-relay request_id=req-1]", payload["prompt"])
                self.assertIn("[cc-relay reply_to=req-1]", payload["prompt"])
            finally:
                server.shutdown()
                thread.join()
                server.server_close()

    def test_deliver_no_reply_posts_notice_without_reply_marker(self):
        with tempfile.TemporaryDirectory():
            CaptureHandler.payloads = []
            server = socketserver.TCPServer(("127.0.0.1", 0), CaptureHandler)
            thread = threading.Thread(target=server.serve_forever)
            thread.daemon = True
            thread.start()
            try:
                port = server.server_address[1]
                provider = CCConnectProvider(
                    agent_id="codex-bot",
                    binding={
                        "webhook_port": port,
                        "webhook_path": "/hook",
                        "session_key": "feishu:s1:u1",
                        "delivery_ack_timeout": 0,
                    },
                )
                envelope = RelayEnvelope(
                    request_id="notice-1",
                    sender="hub",
                    target="codex-bot",
                    body="status update",
                    created_at=1.0,
                    reply_to=None,
                    ttl=30,
                    expect_reply=False,
                )

                receipt = provider.deliver(envelope)

                self.assertEqual(receipt.status, "delivered")
                payload = CaptureHandler.payloads[-1]
                self.assertIn("status update", payload["prompt"])
                self.assertIn("[cc-relay notice_id=notice-1]", payload["prompt"])
                self.assertNotIn("[cc-relay request_id=notice-1]", payload["prompt"])
                self.assertNotIn("[cc-relay reply_to=notice-1]", payload["prompt"])
            finally:
                server.shutdown()
                thread.join()
                server.server_close()

    def test_http_accepted_without_session_marker_is_not_delivery(self):
        with tempfile.TemporaryDirectory() as data_dir:
            sessions = Path(data_dir) / "sessions"
            sessions.mkdir()
            (sessions / "codex-bot_active.json").write_text(
                json.dumps({"sessions": {"s1": {"history": []}}}),
                encoding="utf-8",
            )
            CaptureHandler.payloads = []
            server = socketserver.TCPServer(("127.0.0.1", 0), CaptureHandler)
            thread = threading.Thread(target=server.serve_forever)
            thread.daemon = True
            thread.start()
            try:
                provider = CCConnectProvider(
                    agent_id="codex-bot",
                    binding={
                        "webhook_port": server.server_address[1],
                        "webhook_path": "/hook",
                        "session_key": "feishu:s1:u1",
                        "data_dir": data_dir,
                        "delivery_ack_timeout": 0.1,
                    },
                )
                envelope = RelayEnvelope(
                    request_id="busy-drop",
                    sender="hub",
                    target="codex-bot",
                    body="reply",
                    created_at=1.0,
                    reply_to=None,
                    ttl=30,
                    expect_reply=False,
                )

                receipt = provider.deliver(envelope)

                self.assertEqual(receipt.status, "failed")
                self.assertIn("not acknowledged", receipt.error)
                self.assertEqual(len(CaptureHandler.payloads), 1)
            finally:
                server.shutdown()
                thread.join()
                server.server_close()

    def test_session_marker_confirms_delivery_and_prevents_duplicate_post(self):
        with tempfile.TemporaryDirectory() as data_dir:
            sessions = Path(data_dir) / "sessions"
            sessions.mkdir()
            session_path = sessions / "codex-bot_active.json"
            session_path.write_text(
                json.dumps({"sessions": {"s1": {"history": []}}}),
                encoding="utf-8",
            )

            class AcceptingHandler(CaptureHandler):
                payloads = []

                def do_POST(self):  # noqa: N802
                    length = int(self.headers["Content-Length"])
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    AcceptingHandler.payloads.append(payload)
                    data = json.loads(session_path.read_text(encoding="utf-8"))
                    data["sessions"]["s1"]["history"].append({
                        "role": "user",
                        "content": payload["prompt"],
                        "timestamp": "2026-07-25T10:00:00-04:00",
                    })
                    session_path.write_text(json.dumps(data), encoding="utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"event":"cc-relay","status":"accepted"}')

            server = socketserver.TCPServer(("127.0.0.1", 0), AcceptingHandler)
            thread = threading.Thread(target=server.serve_forever)
            thread.daemon = True
            thread.start()
            try:
                provider = CCConnectProvider(
                    agent_id="codex-bot",
                    binding={
                        "webhook_port": server.server_address[1],
                        "webhook_path": "/hook",
                        "session_key": "feishu:s1:u1",
                        "data_dir": data_dir,
                        "delivery_ack_timeout": 0.2,
                    },
                )
                envelope = RelayEnvelope(
                    request_id="confirmed",
                    sender="hub",
                    target="codex-bot",
                    body="reply",
                    created_at=1.0,
                    reply_to=None,
                    ttl=30,
                    expect_reply=False,
                )

                first = provider.deliver(envelope)
                second = provider.deliver(envelope)

                self.assertEqual(first.status, "delivered")
                self.assertEqual(second.status, "delivered")
                self.assertEqual(len(AcceptingHandler.payloads), 1)
            finally:
                server.shutdown()
                thread.join()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
