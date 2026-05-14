"""Tests for providers.cdp_provider — CDP MessageProvider adapter."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.envelope import RelayEnvelope
from providers.cdp_provider import CDPProvider


def _make_envelope(request_id="req-123"):
    return RelayEnvelope(
        request_id=request_id,
        sender="test-sender",
        target="test-target",
        body="Hello, please fix the bug.",
        created_at=1000.0,
        reply_to=None,
    )


def _make_binding(backend="antigravity", port=9000):
    return {"backend": backend, "cdp_port": port}


class CDPProviderDeliverTest(unittest.TestCase):
    @patch("providers.cdp_provider.CDPProvider._ensure_backend")
    def test_deliver_sends_message_and_returns_receipt(self, mock_ensure):
        mock_backend = MagicMock()
        mock_backend.send_message.return_value = "SENT"
        mock_ensure.return_value = mock_backend

        provider = CDPProvider("test-agent", _make_binding())
        envelope = _make_envelope()
        receipt = provider.deliver(envelope)

        self.assertEqual(receipt.status, "delivered")
        self.assertEqual(receipt.provider, "cdp")
        self.assertEqual(receipt.request_id, "req-123")
        mock_backend.send_message.assert_called_once()
        # Verify the prompt contains relay markers
        call_args = mock_backend.send_message.call_args[0][0]
        self.assertIn("[cc-relay request_id=req-123]", call_args)
        self.assertIn("[cc-relay reply_to=req-123]", call_args)

    @patch("providers.cdp_provider.CDPProvider._ensure_backend")
    def test_deliver_returns_failed_on_send_error(self, mock_ensure):
        mock_backend = MagicMock()
        mock_backend.send_message.return_value = "NO_INPUT"
        mock_ensure.return_value = mock_backend

        provider = CDPProvider("test-agent", _make_binding())
        receipt = provider.deliver(_make_envelope())

        self.assertEqual(receipt.status, "failed")
        self.assertEqual(receipt.error, "NO_INPUT")

    @patch("providers.cdp_provider.CDPProvider._ensure_backend")
    def test_deliver_returns_failed_on_connection_error(self, mock_ensure):
        mock_ensure.side_effect = ConnectionError("CDP not running")

        provider = CDPProvider("test-agent", _make_binding())
        receipt = provider.deliver(_make_envelope())

        self.assertEqual(receipt.status, "failed")
        self.assertIn("CDP not running", receipt.error)


class CDPProviderPollEventsTest(unittest.TestCase):
    def test_returns_empty_when_no_pending_request(self):
        provider = CDPProvider("test-agent", _make_binding())
        events = provider.poll_events()
        self.assertEqual(events, [])

    @patch("providers.cdp_provider.CDPProvider._ensure_backend")
    def test_returns_reply_when_marker_found(self, mock_ensure):
        mock_backend = MagicMock()
        mock_backend.poll_once.return_value = (
            [],
            "Some output.\n[cc-relay reply_to=req-123]\nThe fix is done.",
        )
        mock_backend.state = MagicMock()
        mock_backend.state.consecutive_idle = 0
        mock_ensure.return_value = mock_backend

        provider = CDPProvider("test-agent", _make_binding())
        provider._pending_request_id = "req-123"

        events = provider.poll_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, "message.reply")
        self.assertEqual(events[0].request_id, "req-123")
        # Content should start with the marker so match.py can extract it
        self.assertTrue(events[0].content.startswith("[cc-relay reply_to=req-123]"))
        self.assertIn("The fix is done.", events[0].content)

    @patch("providers.cdp_provider.CDPProvider._ensure_backend")
    def test_returns_empty_when_no_marker_yet(self, mock_ensure):
        mock_backend = MagicMock()
        mock_backend.poll_once.return_value = ([], "Still working on it...")
        mock_backend.state = MagicMock()
        mock_backend.state.consecutive_idle = 0
        mock_ensure.return_value = mock_backend

        provider = CDPProvider("test-agent", _make_binding())
        provider._pending_request_id = "req-123"

        events = provider.poll_events()
        self.assertEqual(events, [])

    @patch("providers.cdp_provider.CDPProvider._ensure_backend")
    def test_clicks_hitl_after_idle_threshold(self, mock_ensure):
        mock_backend = MagicMock()
        mock_backend.poll_once.return_value = ([], "Same text as before")
        mock_backend.state = MagicMock()
        mock_backend.state.consecutive_idle = 5  # Above threshold
        mock_ensure.return_value = mock_backend

        provider = CDPProvider("test-agent", _make_binding())
        provider._pending_request_id = "req-123"

        provider.poll_events()
        mock_backend.click_hitl_buttons.assert_called_once()

    @patch("providers.cdp_provider.CDPProvider._ensure_backend")
    def test_clears_pending_after_reply(self, mock_ensure):
        mock_backend = MagicMock()
        mock_backend.poll_once.return_value = (
            [],
            "[cc-relay reply_to=req-123]\nDone.",
        )
        mock_backend.state = MagicMock()
        mock_backend.state.consecutive_idle = 0
        mock_ensure.return_value = mock_backend

        provider = CDPProvider("test-agent", _make_binding())
        provider._pending_request_id = "req-123"

        provider.poll_events()
        self.assertIsNone(provider._pending_request_id)


class CDPProviderGetHealthTest(unittest.TestCase):
    @patch("providers.cdp_provider.urllib.request.urlopen")
    def test_health_up(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"Browser": "Electron/1.0"}).encode()
        mock_urlopen.return_value = mock_resp

        with patch.object(CDPProvider, "_ensure_backend") as mock_ensure:
            mock_backend = MagicMock()
            mock_backend.list_targets.return_value = [{"type": "page", "url": "workbench"}]
            mock_backend.find_target.return_value = {"type": "page", "url": "workbench"}
            mock_ensure.return_value = mock_backend

            provider = CDPProvider("test-agent", _make_binding())
            health = provider.get_health()

        self.assertEqual(health.provider_status, "up")
        self.assertEqual(health.agent_status, "idle")
        self.assertIn("Electron", health.details)

    @patch("providers.cdp_provider.urllib.request.urlopen")
    def test_health_down_when_cdp_unreachable(self, mock_urlopen):
        mock_urlopen.side_effect = ConnectionError("Connection refused")

        provider = CDPProvider("test-agent", _make_binding())
        health = provider.get_health()

        self.assertEqual(health.provider_status, "down")
        self.assertEqual(health.agent_status, "unreachable")


class CDPProviderRegistryTest(unittest.TestCase):
    def test_get_provider_returns_cdp_provider(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from hub import get_provider

        agent = {
            "name": "test-ide",
            "provider": "cdp",
            "binding": _make_binding(),
        }
        provider = get_provider(agent)
        self.assertIsInstance(provider, CDPProvider)

    def test_cdp_subcommand_registered(self):
        from hub import parse_args

        args = parse_args(["cdp", "status", "my-agent"])
        self.assertEqual(args.command, "cdp")
        self.assertEqual(args.cdp_command, "status")
        self.assertEqual(args.agent, "my-agent")


if __name__ == "__main__":
    unittest.main()
