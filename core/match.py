import time
import re


_REPLY_MARKER_RE = re.compile(r"^\s*\[cc-relay\s+reply_to=([A-Za-z0-9_.:-]+)\]\s*")
# Agents sometimes emit prose before the reply marker; accept a marker anywhere
# in the message so those replies are not dropped as "missing_relay_marker".
_REPLY_MARKER_ANYWHERE_RE = re.compile(r"\[cc-relay\s+reply_to=([A-Za-z0-9_.:-]+)\]\s*")


def parse_relay_reply(content):
    text = content or ""
    match = _REPLY_MARKER_RE.match(text)
    if match:
        reply = text[match.end():].lstrip("\r\n")
        return {
            "request_id": match.group(1),
            "content": reply,
        }
    match = _REPLY_MARKER_ANYWHERE_RE.search(text)
    if not match:
        return None
    preamble = text[:match.start()].strip()
    reply = text[match.end():].lstrip("\r\n")
    if preamble:
        reply = preamble + "\n\n" + reply if reply else preamble
    return {
        "request_id": match.group(1),
        "content": reply,
    }


def extract_relay_reply(content, request_id):
    parsed = parse_relay_reply(content)
    if not parsed or parsed["request_id"] != request_id:
        return None
    return parsed["content"]


def find_request_for_session(store, session_key):
    lock = store.get_active_lock(session_key)
    if lock:
        return store.get_message(lock["request_id"])
    return store.find_latest_open_message_by_session(session_key)


def wait_for_reply_framework(store, provider, request_id, session_key, timeout_secs, poll_interval):
    deadline = time.time() + max(float(timeout_secs), 0.0)
    message = store.get_message(request_id)
    delivered_at = 0.0
    if message and message.get("delivered_at") is not None:
        delivered_at = float(message["delivered_at"])

    try:
        while time.time() < deadline:
            message = store.get_message(request_id)
            if message and message.get("status") == "replied":
                return message.get("reply_body")

            events = provider.poll_events(cursor=str(delivered_at))
            for event in events:
                reply = extract_relay_reply(event.content, request_id)
                store.append_event(
                    event_type=event.event_type,
                    agent_id=event.agent_id,
                    request_id=request_id if reply is not None else None,
                    session_key=session_key,
                    content=reply if reply is not None else event.content,
                    timestamp=event.timestamp,
                )
                if event.event_type == "message.reply" and reply is not None:
                    store.mark_replied(request_id, reply, event.timestamp)
                    return reply
            time.sleep(max(float(poll_interval), 0.01))
        store.mark_timeout(request_id, time.time())
        return None
    finally:
        store.release_session_lock(session_key, request_id)
