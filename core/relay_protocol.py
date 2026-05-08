"""Shared relay protocol functions used by both cc_connect and cdp providers."""

from __future__ import annotations

import re

_CDP_MARKER_RE = re.compile(r"\[cc-relay\s+reply_to=([A-Za-z0-9_.:-]+)\]")
_INSTRUCTION_PREFIX = "Then put your answer"


def build_relay_prompt(envelope):
    """Build a relay prompt with request/reply markers.

    Moved from providers/cc_connect.py so both providers share it.
    """
    return "\n".join([
        "[cc-relay request_id=%s]" % envelope.request_id,
        "",
        envelope.body,
        "",
        "Relay protocol:",
        "When you answer this request, start your final response with exactly this line:",
        "[cc-relay reply_to=%s]" % envelope.request_id,
        "Then put your answer after that marker. Do not use this marker for any other conversation.",
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
        after = text[match.end():].lstrip("\r\n")
        # Skip prompt markers — they're followed by the instruction text
        if after.startswith(_INSTRUCTION_PREFIX):
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
