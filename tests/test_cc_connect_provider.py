import http.server
import json
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

    def test_deliver_posts_webhook_payload(self):
        with tempfile.TemporaryDirectory():
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
                self.assertEqual(payload["event"], "cc-relay")
                self.assertIn("ping", payload["prompt"])
                self.assertIn("[cc-relay request_id=req-1]", payload["prompt"])
                self.assertIn("[cc-relay reply_to=req-1]", payload["prompt"])
            finally:
                server.shutdown()
                thread.join()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
