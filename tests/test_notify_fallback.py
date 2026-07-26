import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock
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


class SessionActivityStub(object):
    def __init__(self, status="idle", idle_token="idle-1", marker=False):
        self.status = status
        self.idle_token = idle_token
        self.marker = marker

    def get_session_activity(self):
        return {
            "status": self.status,
            "idle_token": self.idle_token if self.status == "idle" else None,
            "last_seen_at": 1.0,
        }

    def has_delivery_marker(self, request_id, expect_reply=False):
        return self.marker


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
        self.session_activity = SessionActivityStub()
        self.activity_patcher = mock.patch.object(
            hub,
            "_origin_session_provider",
            return_value=self.session_activity,
        )
        self.activity_patcher.start()

    def tearDown(self):
        self.activity_patcher.stop()
        self.tmpdir.cleanup()

    def test_webhook_injection_preferred_over_config_send(self):
        """Webhook must be tried first: it injects the reply into the origin
        agent conversation, while `cc-connect send` only posts to the
        user-facing chat and the origin agent never sees the reply."""
        cli_calls = []

        def runner(command, input_text):
            cli_calls.append(command)
            return CompletedProcessStub()

        with mock.patch.object(
            hub,
            "_send_via_origin_webhook",
            return_value={"status": "sent", "via": "webhook"},
        ) as webhook_mock:
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
        self.assertEqual(result["notify"]["status"], "sent")
        self.assertEqual(result["notify"].get("via"), "outbox")
        webhook_mock.assert_called_once()
        # config send must not fire when webhook injection succeeded
        self.assertEqual(cli_calls, [])

    def test_busy_webhook_failure_stays_queued_without_config_fallback(self):
        """A failed agent injection must stay queued. Posting the reply to the
        human chat through config send is not an acknowledgement that the
        origin agent received it."""
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
        self.assertEqual(result["notify"]["status"], "queued")
        self.assertEqual(cli_calls, [])
        self.assertEqual(len(self.store.list_notifications(status="pending")), 1)

    def test_webhook_network_error_does_not_fall_back_to_config(self):
        """The durable outbox retries the only path that reaches the agent."""
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
        self.assertEqual(result["notify"]["status"], "queued")
        self.assertEqual(cli_calls, [])
        self.assertIn("Connection refused", result["notify"]["error"])

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
        self.assertIn("webhook_missing", message["notify_error"])

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
        notify = result["notify"]
        self.assertEqual(notify["status"], "queued")

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
        self.assertEqual(result["notify"]["status"], "queued")

    def test_origin_webhook_notification_is_notice_not_request(self):
        """The reply forwarded to the origin must be a notice (expect_reply
        False). With request framing the origin is ordered to answer with a
        reply marker whose request_id was never registered in state.db, so
        its answer is dropped as unmatched and the relay thread dies
        silently while the origin believes it replied."""
        from core.envelope import DeliveryReceipt
        from core.relay_protocol import build_relay_prompt

        captured = {}

        class ProviderStub(object):
            def __init__(self, agent_id, binding):
                captured["agent_id"] = agent_id

            def deliver(self, envelope):
                captured["envelope"] = envelope
                return DeliveryReceipt(
                    request_id=envelope.request_id,
                    status="delivered",
                    provider="cc_connect",
                    delivered_at=12.0,
                )

        with mock.patch.object(hub, "CCConnectProvider", ProviderStub):
            result = hub._send_via_origin_webhook(
                "codex-bot",
                "feishu:origin:u1",
                {"webhook_port": 9999, "session_key": "feishu:origin:u1"},
                "[cc-relay:claude-bot]\npong",
            )

        self.assertEqual(result["status"], "sent")
        envelope = captured["envelope"]
        self.assertFalse(envelope.expect_reply)
        prompt = build_relay_prompt(envelope)
        self.assertIn("notice_id=", prompt)
        self.assertNotIn("request_id=", prompt)

    def test_busy_reply_is_delivered_from_outbox_after_idle_signal(self):
        with mock.patch.object(
            hub,
            "_send_via_origin_webhook",
            return_value={
                "status": "failed",
                "error": "delivery not acknowledged: session busy",
            },
        ):
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
                        "codex-bot": {"webhook_port": 9999},
                    }
                },
            )

        self.assertEqual(result["notify"]["status"], "queued")
        pending = self.store.list_notifications(status="pending")
        self.assertEqual(len(pending), 1)
        self.assertIn("session busy", pending[0]["last_error"])
        self.session_activity.idle_token = "idle-2"

        notice_ids = []

        def delivered(origin_project, origin_session, binding, content, notice_id=None):
            notice_ids.append(notice_id)
            return {"status": "sent", "via": "webhook"}

        with mock.patch.object(hub, "_send_via_origin_webhook", side_effect=delivered):
            drained = hub.drain_notification_outbox(
                state_path=self.db_path,
                bindings={
                    "cc_connect": {
                        "codex-bot": {"webhook_port": 9999},
                    }
                },
                origin_project="codex-bot",
                origin_session="feishu:origin:u1",
                force=True,
                now=20.0,
            )

        self.assertEqual(drained[0]["status"], "delivered")
        self.assertTrue(notice_ids[0].startswith("outbox-"))
        self.assertEqual(
            self.store.get_notification(pending[0]["id"])["status"],
            "delivered",
        )
        self.assertEqual(self.store.get_message("req-1")["notified_at"], 20.0)

    def test_outbox_coalesces_same_session_fifo_into_one_attempt(self):
        base = {
            "request_id": "req-1",
            "origin_project": "codex-bot",
            "origin_session_key": "feishu:origin:u1",
            "target": "claude-bot",
        }
        first = hub.enqueue_reply_notification(
            self.store, base, "first", now=10.0
        )
        second = hub.enqueue_reply_notification(
            self.store, base, "second", label="follow-up", now=11.0
        )
        calls = []

        def still_busy(*args, **kwargs):
            calls.append((args, kwargs))
            return {"status": "failed", "error": "session busy"}

        with mock.patch.object(hub, "_send_via_origin_webhook", side_effect=still_busy):
            drained = hub.drain_notification_outbox(
                state_path=self.db_path,
                bindings={
                    "cc_connect": {
                        "codex-bot": {"webhook_port": 9999},
                    }
                },
                origin_project="codex-bot",
                origin_session="feishu:origin:u1",
                force=True,
                now=12.0,
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            [item["id"] for item in drained],
            [first["id"], second["id"]],
        )
        batch_content = calls[0][0][3]
        self.assertLess(batch_content.index("first"), batch_content.index("second"))
        self.assertIn(
            "[cc-relay notice_id=%s]" % hub._notification_notice_id(first),
            batch_content,
        )
        self.assertIn(
            "[cc-relay notice_id=%s]" % hub._notification_notice_id(second),
            batch_content,
        )
        self.assertEqual(
            self.store.get_notification(second["id"])["attempts"],
            1,
        )
        self.assertEqual(drained[0]["batch_size"], 2)
        self.assertEqual(drained[1]["batch_size"], 2)

    def test_successful_batch_delivers_all_items_in_one_synthetic_turn(self):
        base = {
            "request_id": "req-1",
            "origin_project": "codex-bot",
            "origin_session_key": "feishu:origin:u1",
            "target": "claude-bot",
        }
        first = hub.enqueue_reply_notification(
            self.store, base, "first result", now=10.0
        )
        second = hub.enqueue_reply_notification(
            self.store, base, "second result", label="follow-up", now=11.0
        )

        with mock.patch.object(
            hub,
            "_send_via_origin_webhook",
            return_value={"status": "sent", "via": "webhook"},
        ) as webhook_mock:
            drained = hub.drain_notification_outbox(
                state_path=self.db_path,
                bindings={
                    "cc_connect": {
                        "codex-bot": {"webhook_port": 9999},
                    }
                },
                force=True,
                now=12.0,
            )

        webhook_mock.assert_called_once()
        self.assertEqual(
            [item["status"] for item in drained],
            ["delivered", "delivered"],
        )
        self.assertEqual(
            self.store.get_notification(first["id"])["status"],
            "delivered",
        )
        self.assertEqual(
            self.store.get_notification(second["id"])["status"],
            "delivered",
        )

    def test_known_busy_session_is_queued_without_webhook_probe(self):
        self.session_activity.status = "busy"
        item = hub.enqueue_reply_notification(
            self.store,
            {
                "request_id": "req-1",
                "origin_project": "codex-bot",
                "origin_session_key": "feishu:origin:u1",
                "target": "claude-bot",
            },
            "wait for idle",
            now=10.0,
        )

        with mock.patch.object(hub, "_send_via_origin_webhook") as webhook_mock:
            drained = hub.drain_notification_outbox(
                state_path=self.db_path,
                bindings={
                    "cc_connect": {
                        "codex-bot": {"webhook_port": 9999},
                    }
                },
                force=True,
                now=12.0,
            )

        webhook_mock.assert_not_called()
        self.assertEqual(drained[0]["status"], "deferred")
        self.assertEqual(drained[0]["reason"], "session busy")
        pending = self.store.get_notification(item["id"])
        self.assertEqual(pending["attempts"], 0)
        self.assertIsNone(pending["last_attempt_idle_token"])

    def test_same_idle_generation_never_posts_twice(self):
        item = hub.enqueue_reply_notification(
            self.store,
            {
                "request_id": "req-1",
                "origin_project": "codex-bot",
                "origin_session_key": "feishu:origin:u1",
                "target": "claude-bot",
            },
            "one attempt per idle edge",
            now=10.0,
        )
        failed = {
            "status": "failed",
            "error": "delivery not acknowledged: session busy",
        }
        with mock.patch.object(
            hub,
            "_send_via_origin_webhook",
            return_value=failed,
        ) as webhook_mock:
            first = hub.drain_notification_outbox(
                state_path=self.db_path,
                bindings={
                    "cc_connect": {
                        "codex-bot": {"webhook_port": 9999},
                    }
                },
                force=True,
                now=12.0,
            )
            second = hub.drain_notification_outbox(
                state_path=self.db_path,
                bindings={
                    "cc_connect": {
                        "codex-bot": {"webhook_port": 9999},
                    }
                },
                force=True,
                now=13.0,
            )

        self.assertEqual(webhook_mock.call_count, 1)
        self.assertEqual(first[0]["status"], "queued")
        self.assertEqual(second[0]["status"], "deferred")
        self.assertEqual(second[0]["reason"], "idle generation already attempted")
        pending = self.store.get_notification(item["id"])
        self.assertEqual(pending["attempts"], 1)
        self.assertEqual(pending["last_attempt_idle_token"], "idle-1")

    def test_new_idle_generation_allows_exactly_one_retry(self):
        item = hub.enqueue_reply_notification(
            self.store,
            {
                "request_id": "req-1",
                "origin_project": "codex-bot",
                "origin_session_key": "feishu:origin:u1",
                "target": "claude-bot",
            },
            "retry after final",
            now=10.0,
        )
        outcomes = [
            {"status": "failed", "error": "session busy"},
            {"status": "sent", "via": "webhook"},
        ]
        with mock.patch.object(
            hub,
            "_send_via_origin_webhook",
            side_effect=outcomes,
        ) as webhook_mock:
            hub.drain_notification_outbox(
                state_path=self.db_path,
                bindings={
                    "cc_connect": {
                        "codex-bot": {"webhook_port": 9999},
                    }
                },
                force=True,
                now=12.0,
            )
            self.session_activity.idle_token = "idle-2"
            drained = hub.drain_notification_outbox(
                state_path=self.db_path,
                bindings={
                    "cc_connect": {
                        "codex-bot": {"webhook_port": 9999},
                    }
                },
                force=True,
                now=20.0,
            )

        self.assertEqual(webhook_mock.call_count, 2)
        self.assertEqual(drained[0]["status"], "delivered")
        delivered = self.store.get_notification(item["id"])
        self.assertEqual(delivered["attempts"], 2)
        self.assertEqual(delivered["last_attempt_idle_token"], "idle-2")

    def test_late_marker_acknowledges_without_duplicate_post(self):
        item = hub.enqueue_reply_notification(
            self.store,
            {
                "request_id": "req-1",
                "origin_project": "codex-bot",
                "origin_session_key": "feishu:origin:u1",
                "target": "claude-bot",
            },
            "late ack",
            now=10.0,
        )
        self.store.mark_notification_failed(
            item["id"],
            "ack timeout",
            attempted_at=12.0,
            next_attempt_at=13.0,
            idle_token="idle-1",
        )
        self.session_activity.marker = True

        with mock.patch.object(hub, "_send_via_origin_webhook") as webhook_mock:
            drained = hub.drain_notification_outbox(
                state_path=self.db_path,
                bindings={
                    "cc_connect": {
                        "codex-bot": {"webhook_port": 9999},
                    }
                },
                force=True,
                now=20.0,
            )

        webhook_mock.assert_not_called()
        self.assertEqual(drained[0]["status"], "delivered")
        self.assertEqual(drained[0]["via"], "late-ack")
        delivered = self.store.get_notification(item["id"])
        self.assertEqual(delivered["attempts"], 1)

    def test_inbox_json_exposes_pending_notifications(self):
        hub.enqueue_reply_notification(
            self.store,
            {
                "request_id": "req-1",
                "origin_project": "codex-bot",
                "origin_session_key": "feishu:origin:u1",
                "target": "claude-bot",
            },
            "reply waiting",
            now=10.0,
        )
        output = io.StringIO()
        args = SimpleNamespace(
            all=False,
            origin_project="codex-bot",
            origin_session="feishu:origin:u1",
            limit=100,
            format="json",
        )
        with mock.patch.object(hub, "STATE_DB_PATH", Path(self.db_path)), \
                redirect_stdout(output):
            exit_code = hub.cmd_inbox(args)

        self.assertEqual(exit_code, 0)
        rows = json.loads(output.getvalue())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending")
        self.assertIn("reply waiting", rows[0]["content"])


if __name__ == "__main__":
    unittest.main()
