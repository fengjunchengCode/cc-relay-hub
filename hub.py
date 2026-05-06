#!/usr/bin/env python3
import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from core.envelope import RelayEnvelope
from core.match import wait_for_reply_framework
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

    info_parser = subparsers.add_parser("info")
    info_parser.add_argument("agent")

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

    if not session_key:
        print("Error: agent %s has no session_key yet. Chat with the bot once first." % agent["name"])
        return 1

    if state.has_active_lock(session_key) and not args.wait:
        print("busy: session %s already has a pending request" % session_key)
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
    )

    if args.wait:
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
            if args.wait:
                state.release_session_lock(session_key, request_id)
            print("Error: %s" % (result.error or "command failed"))
            return 1
        state.mark_delivered(request_id, result.executed_at)
    else:
        receipt = provider.deliver(envelope)
        if receipt.status != "delivered":
            state.mark_failed(request_id, receipt.error or "delivery failed")
            if args.wait:
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
    return 1


if __name__ == "__main__":
    sys.exit(main())
