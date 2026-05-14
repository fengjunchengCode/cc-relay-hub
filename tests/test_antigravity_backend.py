"""Tests for the Antigravity CDP backend selectors."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cdp.backends.antigravity import AntigravityBackend


class AntigravityBackendSelectorTest(unittest.TestCase):
    def test_chat_focus_targets_agent_panel_message_input(self):
        js = AntigravityBackend().get_chat_focus_js()

        self.assertIn(".antigravity-agent-side-panel", js)
        self.assertIn("Message", js)
        self.assertIn("rect.x > 900", js)
        self.assertNotIn("FOCUSED_FALLBACK", js)

    def test_chat_verify_targets_agent_panel_message_input(self):
        js = AntigravityBackend().get_chat_verify_js()

        self.assertIn(".antigravity-agent-side-panel", js)
        self.assertIn("Message", js)
        self.assertIn("rect.x > 900", js)
        self.assertIn("NOT_SENT", js)


if __name__ == "__main__":
    unittest.main()
