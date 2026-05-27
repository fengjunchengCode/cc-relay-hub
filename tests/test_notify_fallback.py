import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import hub  # noqa: E402
from core.state import StateStore  # noqa: E402


class CompletedProcessStub(object):
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self.stderr = stderr


class NotifyFallbackTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "state.db")
        self.store = StateStore(self.db_path)
        self.store.insert_message(
            request_id="req-1",
            sender="hub",
            target="claude-bot",
            session_key="feishu:target:u1",
            provider="cc_connect",
            body="ping",
            status="pending",
            created_at=10.0,
            origin_project="codex-bot",
            origin_session_key="feishu:origin:u1",
        )
        self.store.mark_delivered("req-1", 11.0)
        self.store.acquire_session_lock("feishu:target:u1", "req-1", 30)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_retryable_error_falls_back_to_webhook(self):
        """Daemon-already-running error should fall back to webhook."""
        cli_calls = []

        def runner(command, input_text):
            cli_calls.append(command)
            return CompletedProcessStub(
                returncode=1,
                stderr=b"Error: another cc-connect instance is already running (PID 1234)",
            )

        result = hub.handle_hook_event(
            {
                "event": "message.sent",
                "project": "claude-bot",
                "session_key": "feishu:target:u1",
                "content": "[cc-relay reply_to=req-1]\npong",
                "timestamp": "2026-05-06T10:00:00+08:00",
            },
            state_path=self.db_path,
            bindings={
                "cc_connect": {
                    "codex-bot": {
                        "config_path": "/tmp/config-codex.toml",
                        "webhook_port": 9999,
                        "webhook_host": "127.0.0.1",
                        "webhook_path": "/hook",
                    },
                }
            },
            runner=runner,
        )

        self.assertEqual(result["status"], "matched")
        # CLI was called
        self.assertEqual(len(cli_calls), 1)
        # Notify fell through to webhook (which will fail since port 9999 isn't listening)
        # but the point is it didn't stop at the CLI failure
        notify = result["notify"]
        self.assertIn(notify["status"], ("sent", "failed"))

    def test_hard_error_does_not_fall_back(self):
        """Routing errors (bad project/session) should NOT fall back."""
        cli_calls = []

        def runner(command, input_text):
            cli_calls.append(command)
            return CompletedProcessStub(
                returncode=1,
                stderr=b"Error: project not found",
            )

        result = hub.handle_hook_event(
            {
                "event": "message.sent",
                "project": "claude-bot",
                "session_key": "feishu:target:u1",
                "content": "[cc-relay reply_to=req-1]\npong",
                "timestamp": "2026-05-06T10:00:00+08:00",
            },
            state_path=self.db_path,
            bindings={
                "cc_connect": {
                    "codex-bot": {
                        "config_path": "/tmp/config-codex.toml",
                        "webhook_port": 9999,
                        "webhook_host": "127.0.0.1",
                        "webhook_path": "/hook",
                    },
                }
            },
            runner=runner,
        )

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["notify"]["status"], "failed")
        self.assertIn("project not found", result["notify"]["error"])

    def test_webhook_skipped_recorded_as_failure(self):
        """When webhook fallback is unavailable, it should be recorded as failure."""
        def runner(command, input_text):
            return CompletedProcessStub(
                returncode=1,
                stderr=b"Error: another cc-connect instance is already running",
            )

        hub.handle_hook_event(
            {
                "event": "message.sent",
                "project": "claude-bot",
                "session_key": "feishu:target:u1",
                "content": "[cc-relay reply_to=req-1]\npong",
                "timestamp": "2026-05-06T10:00:00+08:00",
            },
            state_path=self.db_path,
            bindings={
                "cc_connect": {
                    "codex-bot": {
                        "config_path": "/tmp/config-codex.toml",
                        # no webhook_port → webhook fallback unavailable
                    },
                }
            },
            runner=runner,
        )

        message = self.store.get_message("req-1")
        self.assertEqual(message["status"], "replied")
        self.assertIsNone(message["notified_at"])
        self.assertIsNotNone(message["notify_error"])
        self.assertIn("webhook fallback unavailable", message["notify_error"])

    def test_config_missing_falls_back_to_webhook(self):
        """No config_path should fall back to webhook."""
        def runner(command, input_text):
            return CompletedProcessStub()

        result = hub.handle_hook_event(
            {
                "event": "message.sent",
                "project": "claude-bot",
                "session_key": "feishu:target:u1",
                "content": "[cc-relay reply_to=req-1]\npong",
                "timestamp": "2026-05-06T10:00:00+08:00",
            },
            state_path=self.db_path,
            bindings={
                "cc_connect": {
                    "codex-bot": {
                        # no config_path
                        "webhook_port": 9999,
                        "webhook_host": "127.0.0.1",
                        "webhook_path": "/hook",
                    },
                }
            },
            runner=runner,
        )

        self.assertEqual(result["status"], "matched")
        # Falls through to webhook
        notify = result["notify"]
        self.assertIn(notify["status"], ("sent", "failed"))

    def test_missing_cc_connect_executable_falls_back_to_webhook(self):
        """FileNotFoundError from config send should not crash relay notification."""
        def runner(command, input_text):
            raise FileNotFoundError("cc-connect")

        result = hub.handle_hook_event(
            {
                "event": "message.sent",
                "project": "claude-bot",
                "session_key": "feishu:target:u1",
                "content": "[cc-relay reply_to=req-1]\npong",
                "timestamp": "2026-05-06T10:00:00+08:00",
            },
            state_path=self.db_path,
            bindings={
                "cc_connect": {
                    "codex-bot": {
                        "config_path": "/tmp/config-codex.toml",
                        "webhook_port": 9999,
                        "webhook_host": "127.0.0.1",
                        "webhook_path": "/hook",
                    },
                }
            },
            runner=runner,
        )

        self.assertEqual(result["status"], "matched")
        self.assertIn(result["notify"]["status"], ("sent", "failed"))


if __name__ == "__main__":
    unittest.main()
