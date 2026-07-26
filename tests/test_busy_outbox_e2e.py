import http.server
import json
import os
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.state import StateStore  # noqa: E402


def _node_available():
    try:
        subprocess.run(["node", "--version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    try:
        return sock.getsockname()[1]
    finally:
        sock.close()


def _wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class BusyThenAcceptHandler(http.server.BaseHTTPRequestHandler):
    payloads = []
    session_path = None

    def do_POST(self):  # noqa: N802
        length = int(self.headers["Content-Length"])
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.__class__.payloads.append(payload)
        # The hub must reach this handler only after the persisted origin
        # session becomes idle. Accepted delivery writes the stable notice
        # marker into history as cc-connect does.
        data = json.loads(self.__class__.session_path.read_text(encoding="utf-8"))
        data["sessions"]["origin"]["history"].append({
            "role": "user",
            "content": payload["prompt"],
            "timestamp": "2026-07-25T10:00:01-04:00",
        })
        self.__class__.session_path.write_text(json.dumps(data), encoding="utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"event":"cc-relay","status":"accepted"}')

    def log_message(self, fmt, *args):  # noqa: A003
        return


@unittest.skipUnless(_node_available(), "node not available")
class BusyOutboxE2ETest(unittest.TestCase):
    def test_busy_reply_survives_and_retries_after_origin_turn_ends(self):
        with tempfile.TemporaryDirectory() as td:
            temp_home = Path(td)
            data_dir = temp_home / ".cc-connect"
            hub_state_dir = data_dir / "cc-relay-hub"
            sessions_dir = data_dir / "sessions"
            hub_state_dir.mkdir(parents=True)
            sessions_dir.mkdir(parents=True)
            session_path = sessions_dir / "codex-bot_active.json"
            session_path.write_text(
                json.dumps({
                    "sessions": {
                        "origin": {
                            "history": [{
                                "role": "user",
                                "content": "origin turn still running",
                                "timestamp": "2026-07-25T09:59:00-04:00",
                            }]
                        }
                    }
                }),
                encoding="utf-8",
            )
            BusyThenAcceptHandler.payloads = []
            BusyThenAcceptHandler.session_path = session_path
            origin_server = socketserver.TCPServer(
                ("127.0.0.1", 0),
                BusyThenAcceptHandler,
            )
            origin_thread = threading.Thread(target=origin_server.serve_forever)
            origin_thread.daemon = True
            origin_thread.start()

            origin_session = "feishu:origin:u1"
            target_session = "feishu:target:u1"
            bindings = {
                "cc_connect": {
                    "codex-bot": {
                        "webhook_host": "127.0.0.1",
                        "webhook_port": origin_server.server_address[1],
                        "webhook_path": "/hook",
                        "session_key": origin_session,
                        "data_dir": str(data_dir),
                        "delivery_ack_timeout": 0.15,
                    }
                },
                "cdp": {},
            }
            (hub_state_dir / "bindings.json").write_text(
                json.dumps(bindings),
                encoding="utf-8",
            )
            store = StateStore(str(hub_state_dir / "state.db"))
            store.insert_message(
                request_id="req-e2e",
                sender="hub",
                target="my-project",
                session_key=target_session,
                provider="cc_connect",
                body="work",
                status="pending",
                created_at=10.0,
                origin_project="codex-bot",
                origin_session_key=origin_session,
            )
            store.mark_delivered("req-e2e", 11.0)

            hook_port = _free_port()
            env = dict(os.environ)
            env["HOME"] = str(temp_home)
            env["HOOK_PORT"] = str(hook_port)
            env["HOOK_EVENTS_FILE"] = str(temp_home / "events.jsonl")
            env["IDLE_DRAIN_DELAY_MS"] = "50"
            env["STARTUP_DRAIN_DELAY_MS"] = "10000"
            env["RECONCILE_INTERVAL_SECS"] = "0"
            env["OUTBOX_WATCHDOG_INTERVAL_SECS"] = "0"
            env["PYTHONPATH"] = os.pathsep.join(
                path for path in sys.path if path
            )
            hook_log = temp_home / "hook.log"
            hook_log_handle = hook_log.open("w", encoding="utf-8")
            server = subprocess.Popen(
                ["node", str(ROOT / "hook-server.mjs")],
                cwd=str(ROOT),
                env=env,
                stdout=hook_log_handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                self.assertTrue(
                    _wait_until(
                        lambda: _port_open(hook_port),
                        timeout=5,
                    ),
                    "hook server did not start",
                )
                self._post_hook(
                    hook_port,
                    {
                        "event": "message.sent",
                        "project": "my-project",
                        "session_key": target_session,
                        "content": "[cc-relay reply_to=req-e2e]\nfinished",
                    },
                )
                queued = _wait_until(
                    lambda: len(store.list_notifications(status="pending")) == 1,
                    timeout=4,
                )
                if not queued:
                    self.fail(
                        "busy delivery was not persisted: outbox=%r message=%r "
                        "webhook_posts=%r server_exit=%r"
                        % (
                            store.list_notifications(),
                            store.get_message("req-e2e"),
                            BusyThenAcceptHandler.payloads,
                            (server.poll(), hook_log.read_text(encoding="utf-8")),
                        )
                    )
                # Busy is checked before POST: no speculative webhook request.
                self.assertEqual(len(BusyThenAcceptHandler.payloads), 0)

                # An ordinary final answer is the origin session's idle edge.
                data = json.loads(session_path.read_text(encoding="utf-8"))
                data["sessions"]["origin"]["history"].append({
                    "role": "assistant",
                    "content": "ordinary final answer",
                    "timestamp": "2026-07-25T10:00:00-04:00",
                })
                session_path.write_text(json.dumps(data), encoding="utf-8")
                self._post_hook(
                    hook_port,
                    {
                        "event": "message.sent",
                        "project": "codex-bot",
                        "session_key": origin_session,
                        "content": "ordinary final answer",
                    },
                )
                self.assertTrue(
                    _wait_until(
                        lambda: bool(
                            store.list_notifications(status="delivered")
                        ),
                        timeout=4,
                    ),
                    "end-of-turn drain did not deliver the queued reply",
                )
            finally:
                server.send_signal(signal.SIGTERM)
                server.wait(timeout=5)
                hook_log_handle.close()
                origin_server.shutdown()
                origin_thread.join()
                origin_server.server_close()

            self.assertEqual(len(BusyThenAcceptHandler.payloads), 1)
            marker = BusyThenAcceptHandler.payloads[0]["prompt"].splitlines()[0]
            self.assertIn("[cc-relay notice_id=outbox-", marker)
            self.assertIsNotNone(store.get_message("req-e2e")["notified_at"])

    def _post_hook(self, port, payload):
        request = urllib.request.Request(
            "http://127.0.0.1:%d/cc-connect/hooks/reply" % port,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 204)


def _port_open(port):
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=0.2)
        sock.close()
        return True
    except OSError:
        return False


if __name__ == "__main__":
    unittest.main()
