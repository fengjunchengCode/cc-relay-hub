import time


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
                store.append_event(
                    event_type=event.event_type,
                    agent_id=event.agent_id,
                    request_id=request_id,
                    session_key=session_key,
                    content=event.content,
                    timestamp=event.timestamp,
                )
                if event.event_type == "message.reply":
                    store.mark_replied(request_id, event.content, event.timestamp)
                    return event.content
            time.sleep(max(float(poll_interval), 0.01))
        store.mark_timeout(request_id, time.time())
        return None
    finally:
        store.release_session_lock(session_key, request_id)
