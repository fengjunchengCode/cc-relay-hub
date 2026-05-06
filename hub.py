#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from core.envelope import RelayEnvelope
from core.match import find_request_for_session, wait_for_reply_framework
from core.state import StateStore
from providers.cc_connect import CCConnectProvider


DATA_DIR = Path.home() / ".cc-connect"
HUB_DIR = DATA_DIR / "cc-relay-hub"
REGISTRY_PATH = HUB_DIR / "registry.json"
BINDINGS_PATH = HUB_DIR / "bindings.json"
LEGACY_CONFIG_PATH = HUB_DIR / "config.json"
STATE_DB_PATH = HUB_DIR / "state.db"


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="cc-relay", description="Phase 1a CLI for cc-relay")
    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--format", choices=["table", "json"], default="table")

    send_parser = subparsers.add_parser("send")
    send_parser.add_argument("agent")
    send_parser.add_argument("message")
    send_parser.add_argument("--wait", action="store_true")
    send_parser.add_argument("--timeout", type=int, default=300)
    send_parser.add_argument("--origin-project", default=None, help=argparse.SUPPRESS)
    send_parser.add_argument("--origin-session", default=None, help=argparse.SUPPRESS)

    info_parser = subparsers.add_parser("info")
    info_parser.add_argument("agent")

    subparsers.add_parser("_on_hook", help=argparse.SUPPRESS)

    watch_parser = subparsers.add_parser("watch", help="Long-poll for hook events (no shell loop needed)")
    watch_parser.add_argument("--since", default=None, help="ISO-8601 timestamp; only return events after this time")
    watch_parser.add_argument("--timeout", type=int, default=30, help="Per-request timeout in seconds (default: 30)")
    watch_parser.add_argument("--loop", action="store_true", help="Continuously poll until Ctrl-C (default: one-shot)")
    watch_parser.add_argument("--format", choices=["json", "text"], default="json")

    return parser.parse_args(argv)


def load_json(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def find_configs():
    candidates = [
        DATA_DIR / "config.toml",
        DATA_DIR / "config-codex.toml",
        Path("/opt/homebrew/etc/cc-connect/config.toml"),
    ]
    extras = sorted(DATA_DIR.glob("config*.toml"))
    merged = []
    for path in candidates + extras:
        if path.exists() and path not in merged:
            merged.append(path)
    return merged


def session_keys_by_agent():
    session_map = {}
    sessions_dir = DATA_DIR / "sessions"
    if not sessions_dir.exists():
        return session_map
    for session_file in sessions_dir.glob("*.json"):
        stem = session_file.stem
        if "_" not in stem:
            continue
        agent_name = stem.rsplit("_", 1)[0]
        if agent_name not in session_map:
            session_map[agent_name] = stem
    return session_map


def load_legacy_agent_config():
    if not LEGACY_CONFIG_PATH.exists():
        return {}
    data = load_json(LEGACY_CONFIG_PATH)
    return data.get("agents", {})


def bootstrap_registry_and_bindings():
    legacy_agents = load_legacy_agent_config()
    session_map = session_keys_by_agent()
    registry = {"version": 2, "agents": {}}
    bindings = {"cc_connect": {}, "cdp": {}}

    for config_path in find_configs():
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)

        webhook = config.get("webhook", {})
        projects = config.get("projects", [])
        for project in projects:
            name = project.get("name", "unknown")
            agent_cfg = project.get("agent", {})
            options = agent_cfg.get("options", {})
            registry["agents"][name] = {
                "type": agent_cfg.get("type", "unknown"),
                "provider": "cc_connect",
                "work_dir": options.get("work_dir", ""),
                "capabilities": ["message.send", "history.read", "session.control"],
                "labels": [],
            }
            legacy = legacy_agents.get(name, {})
            bindings["cc_connect"][name] = {
                "config_path": str(config_path),
                "webhook_host": "127.0.0.1",
                "webhook_port": int(legacy.get("webhook_port", webhook.get("port", 0))),
                "webhook_path": webhook.get("path", "/hook"),
                "session_key": legacy.get("session_key", session_map.get(name, "")),
            }

    write_json(REGISTRY_PATH, registry)
    write_json(BINDINGS_PATH, bindings)
    return registry, bindings


def ensure_registry_and_bindings():
    if REGISTRY_PATH.exists() and BINDINGS_PATH.exists():
        return load_json(REGISTRY_PATH), load_json(BINDINGS_PATH)
    return bootstrap_registry_and_bindings()


def get_agent(registry, bindings, agent_name):
    agent = registry.get("agents", {}).get(agent_name)
    if not agent:
        raise KeyError("Unknown agent: %s" % agent_name)
    provider_name = agent["provider"]
    binding = bindings.get(provider_name, {}).get(agent_name)
    if not binding:
        raise KeyError("Missing binding for agent: %s" % agent_name)
    combined = dict(agent)
    combined["name"] = agent_name
    combined["binding"] = binding
    return combined


def get_provider(agent):
    if agent["provider"] == "cc_connect":
        return CCConnectProvider(agent["name"], agent["binding"])
    raise ValueError("Unsupported provider: %s" % agent["provider"])


def resolve_origin_context(args):
    origin_project = args.origin_project or os.environ.get("CC_PROJECT") or None
    origin_session = args.origin_session or os.environ.get("CC_SESSION_KEY") or None
    return origin_project, origin_session


def _parse_event_timestamp(value):
    if not value:
        return time.time()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def _build_notification_content(message, reply_text):
    return "[cc-relay:%s]\n%s" % (message["target"], reply_text)


def _run_cc_connect_send(command, input_text):
    return subprocess.run(
        command,
        input=input_text.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def load_bindings(bindings=None):
    if bindings is not None:
        return bindings
    if not BINDINGS_PATH.exists():
        return {"cc_connect": {}, "cdp": {}}
    return load_json(BINDINGS_PATH)


_RETRYABLE_PATTERNS = [
    "already running",
    "another cc-connect instance",
    "ECONNREFUSED",
    "EADDRINUSE",
]


def _is_retryable_error(stderr_text):
    text = stderr_text.lower()
    return any(pat.lower() in text for pat in _RETRYABLE_PATTERNS)


def _send_via_origin_config(origin_project, origin_session, binding, content, runner):
    config_path = binding.get("config_path")
    if not config_path:
        return {"status": "skipped", "reason": "config_missing"}

    command = [
        "cc-connect",
        "--config",
        config_path,
        "send",
        "-p",
        origin_project,
        "-s",
        origin_session,
        "--stdin",
    ]
    result = runner(command, content)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        if _is_retryable_error(stderr):
            return {"status": "skipped", "reason": "retryable", "error": stderr}
        return {"status": "failed", "error": stderr or "cc-connect send failed"}
    return {"status": "sent", "via": "config"}


def _send_via_origin_webhook(origin_project, origin_session, binding, content):
    webhook_port = int(binding.get("webhook_port", 0) or 0)
    if not webhook_port:
        return {"status": "skipped", "reason": "webhook_missing"}

    webhook_binding = dict(binding)
    webhook_binding["session_key"] = origin_session
    provider = CCConnectProvider(origin_project, webhook_binding)
    receipt = provider.deliver(
        RelayEnvelope(
            request_id=uuid.uuid4().hex,
            sender="cc-relay",
            target=origin_project,
            body=content,
            created_at=time.time(),
            reply_to=None,
            ttl=30,
        )
    )
    if receipt.status != "delivered":
        return {"status": "failed", "error": receipt.error or "origin webhook delivery failed"}
    return {"status": "sent", "via": "webhook"}


def notify_origin_reply(message, reply_text, bindings=None, runner=None):
    origin_project = message.get("origin_project")
    origin_session = message.get("origin_session_key")
    if not origin_project or not origin_session:
        return {"status": "skipped", "reason": "origin_missing"}

    bindings = load_bindings(bindings)
    binding = bindings.get("cc_connect", {}).get(origin_project)
    if not binding:
        return {"status": "failed", "error": "origin binding missing"}

    content = _build_notification_content(message, reply_text)
    runner = runner or _run_cc_connect_send
    notify = _send_via_origin_config(origin_project, origin_session, binding, content, runner)
    if notify["status"] == "sent":
        return notify
    if notify["status"] == "failed":
        return notify
    # retryable or config_missing → try webhook fallback
    result = _send_via_origin_webhook(origin_project, origin_session, binding, content)
    if result["status"] == "skipped":
        return {"status": "failed", "error": "webhook fallback unavailable: %s" % result.get("reason", "unknown")}
    return result


def handle_hook_event(payload, state_path=None, bindings=None, runner=None):
    store = StateStore(str(state_path or STATE_DB_PATH))
    event_type = payload.get("event") or payload.get("hook_event") or "unknown"
    agent_id = payload.get("project") or payload.get("agent_id") or "unknown"
    session_key = payload.get("session_key") or ""
    content = payload.get("content") or ""
    timestamp = _parse_event_timestamp(payload.get("timestamp"))
    message = None

    if event_type != "message.sent" or not session_key:
        store.append_event(
            event_type=event_type,
            agent_id=agent_id,
            request_id=None,
            session_key=session_key or None,
            content=content,
            timestamp=timestamp,
        )
        return {"status": "ignored", "reason": "unsupported_event"}

    message = find_request_for_session(store, session_key)
    store.append_event(
        event_type=event_type,
        agent_id=agent_id,
        request_id=message["request_id"] if message else None,
        session_key=session_key,
        content=content,
        timestamp=timestamp,
    )
    if not message:
        return {"status": "unmatched", "session_key": session_key}

    store.mark_replied(message["request_id"], content, timestamp)
    store.release_session_lock(session_key, message["request_id"])

    notify = notify_origin_reply(message, content, bindings=bindings, runner=runner)
    if notify["status"] == "sent":
        store.mark_notified(message["request_id"], time.time())
    elif notify["status"] == "failed":
        store.mark_notify_failed(message["request_id"], notify["error"])

    return {
        "status": "matched",
        "request_id": message["request_id"],
        "notify": notify,
    }


def format_status(agent):
    provider = get_provider(agent)
    health = provider.get_health()
    return {
        "provider_status": health.provider_status,
        "agent_status": health.agent_status,
        "last_seen_at": health.last_seen_at,
        "last_delivery_at": health.last_delivery_at,
        "details": health.details,
    }


def cmd_list(args):
    registry, bindings = ensure_registry_and_bindings()
    agents = []
    for agent_name in sorted(registry.get("agents", {}).keys()):
        agent = get_agent(registry, bindings, agent_name)
        agents.append({
            "name": agent_name,
            "type": agent["type"],
            "provider": agent["provider"],
            "work_dir": agent["work_dir"],
            "webhook_port": agent["binding"].get("webhook_port", 0),
            "session_key": agent["binding"].get("session_key", ""),
        })

    if args.format == "json":
        print(json.dumps(agents, indent=2, ensure_ascii=False))
        return

    print("Found %d agent(s):" % len(agents))
    print("")
    print("  {0:<16} {1:<12} {2:<12} {3:<10} {4}".format(
        "Name", "Type", "Provider", "Webhook", "Session"
    ))
    print("  {0:<16} {1:<12} {2:<12} {3:<10} {4}".format(
        "─" * 16, "─" * 12, "─" * 12, "─" * 10, "─" * 28
    ))
    for item in agents:
        webhook = ":%s" % item["webhook_port"] if item["webhook_port"] else "none"
        session_key = item["session_key"] or "none"
        print("  {0:<16} {1:<12} {2:<12} {3:<10} {4}".format(
            item["name"], item["type"], item["provider"], webhook, session_key
        ))


def cmd_info(args):
    registry, bindings = ensure_registry_and_bindings()
    agent = get_agent(registry, bindings, args.agent)
    status = format_status(agent)

    print("  Agent:       %s" % agent["name"])
    print("  Type:        %s" % agent["type"])
    print("  Provider:    %s" % agent["provider"])
    print("  Work Dir:    %s" % agent["work_dir"])
    print("  Webhook:     %s:%s%s" % (
        agent["binding"].get("webhook_host", "127.0.0.1"),
        agent["binding"].get("webhook_port", 0),
        agent["binding"].get("webhook_path", "/hook"),
    ))
    print("  Session:     %s" % agent["binding"].get("session_key", ""))
    print("  Status:      %s (%s)" % (status["provider_status"], status["agent_status"]))
    print("  Last Seen:   %s" % _format_time(status["last_seen_at"]))
    print("  Details:     %s" % status["details"])


def cmd_send(args):
    registry, bindings = ensure_registry_and_bindings()
    agent = get_agent(registry, bindings, args.agent)
    provider = get_provider(agent)
    state = StateStore(str(STATE_DB_PATH))
    session_key = agent["binding"].get("session_key", "")
    origin_project, origin_session = resolve_origin_context(args)

    if not session_key:
        print("Error: agent %s has no session_key yet. Chat with the bot once first." % agent["name"])
        return 1

    request_id = uuid.uuid4().hex
    now = time.time()
    envelope = RelayEnvelope(
        request_id=request_id,
        sender="cc-relay",
        target=agent["name"],
        body=args.message,
        created_at=now,
        reply_to=None,
        ttl=int(args.timeout),
    )
    state.insert_message(
        request_id=request_id,
        sender=envelope.sender,
        target=envelope.target,
        session_key=session_key,
        provider=agent["provider"],
        body=envelope.body,
        status="pending",
        created_at=now,
        origin_project=origin_project,
        origin_session_key=origin_session,
    )

    locked = state.acquire_session_lock(session_key, request_id, args.timeout)
    if not locked:
        state.mark_failed(request_id, "busy")
        print("busy: session %s already has a pending request" % session_key)
        return 1

    is_control = args.message.startswith("/") and provider.supports_control()
    if is_control:
        result = provider.execute_command(args.message)
        if result.status != "delivered":
            state.mark_failed(request_id, result.error or "command failed")
            state.release_session_lock(session_key, request_id)
            print("Error: %s" % (result.error or "command failed"))
            return 1
        state.mark_delivered(request_id, result.executed_at)
    else:
        receipt = provider.deliver(envelope)
        if receipt.status != "delivered":
            state.mark_failed(request_id, receipt.error or "delivery failed")
            state.release_session_lock(session_key, request_id)
            print("Error: %s" % (receipt.error or "delivery failed"))
            return 1
        state.mark_delivered(request_id, receipt.delivered_at)

    if not args.wait:
        print("Message sent to %s" % agent["name"])
        return 0

    reply = wait_for_reply_framework(
        store=state,
        provider=provider,
        request_id=request_id,
        session_key=session_key,
        timeout_secs=args.timeout,
        poll_interval=1.0,
    )
    if reply is None:
        print("timeout: no reply observed before deadline (Phase 1a best-effort wait)")
        return 1
    print(reply)
    return 0


def _format_time(value):
    if value is None:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))


def cmd_on_hook(_args):
    payload = json.load(sys.stdin)
    result = handle_hook_event(payload)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_watch(args):
    """Long-poll the hook server for new events.

    Default mode: one-shot. Makes a single HTTP request that blocks until
    events arrive (or timeout), prints them, exits.

    --loop mode: continuously long-polls. Each iteration blocks until new
    events arrive, prints them, then immediately long-polls again. Still
    uses a single process -- no shell while-loop needed.
    """
    hook_host = "127.0.0.1"
    hook_port = 9120
    since = args.since or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    timeout_sec = max(1, min(args.timeout, 60))
    loop = args.loop
    fmt = args.format

    def fetch(since_value):
        url = "http://%s:%d/events/longpoll?since=%s&timeout=%d" % (
            hook_host, hook_port,
            urllib.request.quote(since_value, safe=""),
            timeout_sec,
        )
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=timeout_sec + 5) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            print(json.dumps({"error": str(exc.reason)}), file=sys.stderr)
            return None
        except Exception as exc:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
            return None

    while True:
        data = fetch(since)
        if data is None:
            if not loop:
                return 1
            time.sleep(2)
            continue

        events = data.get("events", [])
        if events:
            latest_ts = max(e.get("received_at", "") for e in events)
            if latest_ts:
                since = latest_ts

            if fmt == "json":
                for ev in events:
                    print(json.dumps(ev, ensure_ascii=False))
            else:
                for ev in events:
                    t = ev.get("hook_event", "?")
                    proj = ev.get("payload", {}).get("project", "?")
                    content = ev.get("payload", {}).get("content", "")
                    ts = ev.get("received_at", "")
                    print("[%s] %s from %s: %s" % (ts, t, proj, content[:200]))
            sys.stdout.flush()

        if not loop:
            return 0


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    if not args.command:
        print("Usage: cc-relay <list|send|info> ...")
        return 1
    if args.command == "list":
        cmd_list(args)
        return 0
    if args.command == "send":
        return cmd_send(args)
    if args.command == "info":
        cmd_info(args)
        return 0
    if args.command == "_on_hook":
        return cmd_on_hook(args)
    if args.command == "watch":
        return cmd_watch(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
