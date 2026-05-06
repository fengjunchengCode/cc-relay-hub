import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import hub  # noqa: E402
from core.envelope import ProviderHealth  # noqa: E402


class BootstrapTest(unittest.TestCase):
    def test_subcommand_registered(self):
        args = hub.parse_args(["bootstrap"])
        self.assertEqual(args.command, "bootstrap")

    @patch("hub.bootstrap_registry_and_bindings")
    @patch("hub.get_agent")
    @patch("hub.get_provider")
    def test_returns_zero_when_all_up(self, mock_get_provider, mock_get_agent, mock_bootstrap):
        mock_bootstrap.return_value = (
            {"agents": {"bot-a": {"type": "claude", "provider": "cc_connect", "work_dir": "/tmp"}}},
            {"cc_connect": {"bot-a": {"webhook_port": 9110, "session_key": "s1", "config_path": "/tmp/c.toml"}}},
        )
        mock_get_agent.return_value = {
            "name": "bot-a", "type": "claude", "provider": "cc_connect",
            "work_dir": "/tmp", "binding": {"webhook_port": 9110, "session_key": "s1", "config_path": "/tmp/c.toml"},
        }
        provider = MagicMock()
        provider.get_health.return_value = ProviderHealth(
            provider_status="up", agent_status="idle",
            last_seen_at=None, last_delivery_at=None,
            details="hook=ok, session_file=/tmp/s.json",
        )
        mock_get_provider.return_value = provider

        args = hub.parse_args(["bootstrap"])
        result = hub.cmd_bootstrap(args)
        self.assertEqual(result, 0)

    @patch("hub.bootstrap_registry_and_bindings")
    @patch("hub.get_agent")
    @patch("hub.get_provider")
    def test_returns_one_when_webhook_down(self, mock_get_provider, mock_get_agent, mock_bootstrap):
        mock_bootstrap.return_value = (
            {"agents": {"bot-a": {"type": "claude", "provider": "cc_connect", "work_dir": "/tmp"}}},
            {"cc_connect": {"bot-a": {"webhook_port": 9110, "session_key": "s1", "config_path": "/tmp/c.toml"}}},
        )
        mock_get_agent.return_value = {
            "name": "bot-a", "type": "claude", "provider": "cc_connect",
            "work_dir": "/tmp", "binding": {"webhook_port": 9110, "session_key": "s1", "config_path": "/tmp/c.toml"},
        }
        provider = MagicMock()
        provider.get_health.return_value = ProviderHealth(
            provider_status="down", agent_status="unresponsive",
            last_seen_at=None, last_delivery_at=None,
            details="hook=missing, session_file=missing",
        )
        mock_get_provider.return_value = provider

        args = hub.parse_args(["bootstrap"])
        result = hub.cmd_bootstrap(args)
        self.assertEqual(result, 1)

    @patch("hub.bootstrap_registry_and_bindings")
    def test_returns_one_when_no_agents(self, mock_bootstrap):
        mock_bootstrap.return_value = ({"agents": {}}, {"cc_connect": {}, "cdp": {}})

        args = hub.parse_args(["bootstrap"])
        result = hub.cmd_bootstrap(args)
        self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()
