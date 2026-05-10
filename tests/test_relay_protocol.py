"""Tests for core.relay_protocol — shared relay prompt builder and transcript extractor."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.envelope import RelayEnvelope
from core.relay_protocol import build_relay_prompt, extract_reply_from_transcript


def _make_envelope(request_id="req-123"):
    return RelayEnvelope(
        request_id=request_id,
        sender="test-sender",
        target="test-target",
        body="Hello, please fix the bug.",
        created_at=1000.0,
        reply_to=None,
    )


class BuildRelayPromptTest(unittest.TestCase):
    def test_contains_request_marker(self):
        prompt = build_relay_prompt(_make_envelope("abc-999"))
        self.assertIn("[cc-relay request_id=abc-999]", prompt)

    def test_contains_reply_marker(self):
        prompt = build_relay_prompt(_make_envelope("abc-999"))
        self.assertIn("[cc-relay reply_to=abc-999]", prompt)

    def test_includes_body(self):
        prompt = build_relay_prompt(_make_envelope())
        self.assertIn("Hello, please fix the bug.", prompt)

    def test_includes_protocol_instructions(self):
        prompt = build_relay_prompt(_make_envelope())
        self.assertIn("Relay protocol (mandatory):", prompt)
        self.assertIn("start your final response", prompt)

    def test_protocol_overrides_only_reply_requests(self):
        envelope = _make_envelope()
        envelope.body = "Please only reply pong."
        prompt = build_relay_prompt(envelope)
        self.assertIn("Do not omit the marker", prompt)
        self.assertIn('even if the task asks you to "only reply"', prompt)
        self.assertIn("put the terse answer after the marker", prompt)


class ExtractReplyFromTranscriptTest(unittest.TestCase):
    def test_marker_at_start(self):
        text = "[cc-relay reply_to=req-123]\nHere is the fix."
        result = extract_reply_from_transcript(text, "req-123")
        self.assertEqual(result, "Here is the fix.")

    def test_marker_in_middle(self):
        text = "Some output before.\n[cc-relay reply_to=req-123]\nThe actual reply."
        result = extract_reply_from_transcript(text, "req-123")
        self.assertEqual(result, "The actual reply.")

    def test_marker_at_end(self):
        text = "Lots of output...\n[cc-relay reply_to=req-123]"
        result = extract_reply_from_transcript(text, "req-123")
        self.assertEqual(result, "")

    def test_no_marker(self):
        text = "Just some regular text without any markers."
        result = extract_reply_from_transcript(text, "req-123")
        self.assertIsNone(result)

    def test_wrong_request_id(self):
        text = "[cc-relay reply_to=other-id]\nSome reply."
        result = extract_reply_from_transcript(text, "req-123")
        self.assertIsNone(result)

    def test_empty_text(self):
        result = extract_reply_from_transcript("", "req-123")
        self.assertIsNone(result)

    def test_none_text(self):
        result = extract_reply_from_transcript(None, "req-123")
        self.assertIsNone(result)

    def test_ignores_request_marker(self):
        """Request marker (request_id=X) should not match reply marker (reply_to=X)."""
        text = "[cc-relay request_id=req-123]\nSome output."
        result = extract_reply_from_transcript(text, "req-123")
        self.assertIsNone(result)

    def test_multiple_reply_markers_returns_last(self):
        text = (
            "[cc-relay reply_to=req-123]\nFirst reply.\n"
            "[cc-relay reply_to=req-123]\nSecond reply."
        )
        result = extract_reply_from_transcript(text, "req-123")
        self.assertEqual(result, "Second reply.")

    def test_prompt_then_reply_returns_reply(self):
        """Real-world: prompt contains marker instruction, reply has actual marker."""
        text = (
            "[cc-relay request_id=req-123]\nPlease fix the bug.\n\n"
            "Relay protocol:\n"
            "start your final response with exactly this line:\n"
            "[cc-relay reply_to=req-123]\n"
            "Then put your answer after that marker.\n"
            "undo\nGenerating.\n"
            "[cc-relay reply_to=req-123]\nThe fix is done."
        )
        result = extract_reply_from_transcript(text, "req-123")
        self.assertEqual(result, "The fix is done.")

    def test_truncates_at_ui_boundary(self):
        """Reply followed by UI chrome (timestamps, buttons) is truncated."""
        text = (
            "[cc-relay reply_to=req-123]\nCONFIRM\n\n"
            "12:32\ncontent_copy\nthumb_up\nthumb_down\n"
            "0 Files With Changes\nReview Changes"
        )
        result = extract_reply_from_transcript(text, "req-123")
        self.assertEqual(result, "CONFIRM")

    def test_long_transcript_with_noise(self):
        lines = ["noise line %d" % i for i in range(500)]
        lines.append("[cc-relay reply_to=req-123]")
        lines.append("The actual answer.")
        lines.extend(["more noise %d" % i for i in range(100)])
        text = "\n".join(lines)
        result = extract_reply_from_transcript(text, "req-123")
        # Reply is truncated at first double-newline boundary
        self.assertTrue(result.startswith("The actual answer."))
        # The noise lines are single-newline separated, so all included
        self.assertIn("more noise 99", result)


if __name__ == "__main__":
    unittest.main()
