"""Shared relay protocol functions used by both cc_connect and cdp providers."""

from __future__ import annotations

import re

_CDP_MARKER_RE = re.compile(r"\[cc-relay\s+reply_to=([A-Za-z0-9_.:-]+)\]")
_INSTRUCTION_PREFIX = "Then put your answer"


def build_relay_prompt(envelope):
    """Build a relay prompt with request/reply markers.

    Moved from providers/cc_connect.py so both providers share it.
    """
    if not getattr(envelope, "expect_reply", True):
        return "\n".join([
            "[cc-relay notice_id=%s]" % envelope.request_id,
            "",
            "This is a direct cc-relay-hub notice addressed to you.",
            "No reply is requested for this notice.",
            "Do not start your answer with a cc-relay reply marker.",
            "Do not send a message back only to acknowledge this notice.",
            "",
            "Notice:",
            envelope.body,
            "",
            "If the notice explicitly asks you to take action, do the action quietly unless the sender asks for a result.",
        ])

    return "\n".join([
        "[cc-relay request_id=%s]" % envelope.request_id,
        "",
        "This is a direct cc-relay-hub request addressed to you.",
        "Mandatory transport rule: your visible final answer must start with the reply marker shown below.",
        "Do not answer NO_REPLY or an empty response.",
        "Do not omit the marker even if the task asks you to \"only reply\" with a word or phrase.",
        "",
        "Task:",
        envelope.body,
        "",
        "Relay protocol (mandatory):",
        "When you answer this request, start your final response with exactly this line:",
        "[cc-relay reply_to=%s]" % envelope.request_id,
        "Then put your answer after that marker. If the task asks for a terse answer, put the terse answer after the marker.",
        "Do not use this marker for any other conversation.",
    ])


def extract_reply_from_transcript(text, request_id):
    """Extract reply content from a CDP transcript.

    Unlike core/match.py:extract_relay_reply() which is start-anchored,
    this uses re.search() to find the marker anywhere in the text.
    Returns everything after the LAST matching reply marker, or None if not found.

    The page text contains the original prompt (which includes the marker as an
    instruction) before the actual reply. We skip prompt markers by checking if
    the text after the marker starts with the relay instruction prefix.
    """
    if not text:
        return None
    last_match = None
    for match in _CDP_MARKER_RE.finditer(text):
        if match.group(1) != request_id:
            continue
        after = text[match.end():].lstrip()
        # Skip prompt markers — they're followed by the instruction text
        if after.startswith(_INSTRUCTION_PREFIX) or _INSTRUCTION_PREFIX in after[:200]:
            continue
        last_match = match
    if last_match is not None:
        reply = text[last_match.end():].lstrip("\r\n")
        # The reply is followed by UI chrome (timestamps, buttons, etc.)
        # separated by a double-newline. Truncate at that boundary.
        idx = reply.find("\n\n")
        if idx > 0:
            reply = reply[:idx].rstrip()
        return reply
    return None
