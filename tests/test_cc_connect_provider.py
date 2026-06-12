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


if __name__ == "__main__":
    unittest.main()
