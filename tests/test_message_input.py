import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import hub  # noqa: E402


class MessageInputTest(unittest.TestCase):
    def test_inline_message_still_works(self):
        args = hub.parse_args(["send", "claude-bot", "hello"])

        self.assertEqual(hub._resolve_message_input(args), "hello")

    def test_no_reply_flag_is_available_for_send(self):
        args = hub.parse_args(["send", "claude-bot", "hello", "--no-reply"])

        self.assertTrue(args.no_reply)
        self.assertFalse(args.wait)
        self.assertEqual(hub._resolve_message_input(args), "hello")

    def test_stdin_preserves_multiline_message(self):
        args = hub.parse_args(["send", "claude-bot", "--stdin"])

        with mock.patch("sys.stdin", io.StringIO("line 1\nline 2\n")):
            self.assertEqual(hub._resolve_message_input(args), "line 1\nline 2\n")

    def test_message_file_preserves_multiline_message(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "message.txt"
            path.write_text("alpha\nbeta\n", encoding="utf-8")
            args = hub.parse_args(["relay", "codex", "claude", "--message-file", str(path)])

            self.assertEqual(hub._resolve_message_input(args), "alpha\nbeta\n")

    def test_rejects_multiple_message_sources(self):
        args = hub.parse_args(["send", "claude-bot", "hello", "--stdin"])

        with self.assertRaises(ValueError):
            hub._resolve_message_input(args)

    def test_rejects_missing_message_source(self):
        args = hub.parse_args(["send", "claude-bot"])

        with self.assertRaises(ValueError):
            hub._resolve_message_input(args)

    def test_no_reply_rejects_wait_combination_before_registry_access(self):
        args = hub.parse_args(["send", "claude-bot", "hello", "--no-reply", "--wait"])

        self.assertEqual(hub.cmd_send(args), 1)


if __name__ == "__main__":
    unittest.main()
