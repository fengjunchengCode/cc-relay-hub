#!/usr/bin/env python3
import argparse
import json
import os
import shutil
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
from core.match import parse_relay_reply, wait_for_reply_framework
from core.state import StateStore
from providers.cc_connect import CCConnectProvider, _platform_from_session_key


try:
    import fcntl as _fcntl
except ModuleNotFoundError:
    _fcntl = None
    import msvcrt as _msvcrt
else:
    _msvcrt = None


DATA_DIR = Path.home() / ".cc-connect"
HUB_DIR = DATA_DIR / "cc-relay-hub"
HUB_BIN = HUB_DIR / "bin" / "cc-relay-hub"
REGISTRY_PATH = HUB_DIR / "registry.json"
BINDINGS_PATH = HUB_DIR / "bindings.json"
LEGACY_CONFIG_PATH = HUB_DIR / "config.json"
STATE_DB_PATH = HUB_DIR / "state.db"


def parse_args(argv):
    parser = argparse.ArgumentParser(prog="cc-relay", description="Phase 1a CLI for cc-relay")
    subparsers = parser.add_subparsers(dest="command")

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--format", choices=["table", "json"], default="table")
    list_parser.add_argument("--group", default=None, help="Filter by group name")

    send_parser = subparsers.add_parser("send")
    send_parser.add_argument("agent")
    _add_message_input_args(send_parser)
    send_parser.add_argument("--wait", action="store_true")
    send_parser.add_argument("--timeout", type=int, default=300)
    send_parser.add_argument("--group", default=None, help="Target group for agent lookup")
    send_parser.add_argument("--origin-project", default=None, help=argparse.SUPPRESS)
    send_parser.add_argument("--origin-session", default=None, help=argparse.SUPPRESS)

    info_parser = subparsers.add_parser("info")
    info_parser.add_argument("agent")

    bootstrap_parser = subparsers.add_parser("bootstrap", help="Scan configs, write registry/bindings, verify connectivity")
    bootstrap_sub = bootstrap_parser.add_subparsers(dest="bootstrap_command")
    ctx_parser = bootstrap_sub.add_parser("context", help="Generate agent context files (AGENTS.md, CLAUDE.md, etc.)")
    ctx_parser.add_argument("--write", action="store_true", help="Write context files to disk")
    ctx_parser.add_argument("--check", action="store_true", help="Check if context files are up to date")
    ctx_parser.add_argument("--print", action="store_true", dest="print_mode", help="Print context to stdout")
    ctx_parser.add_argument(
        "--scope",
        choices=["cwd", "global", "workdirs", "all"],
        default="all",
        help="Where to install context files (default: all)",
    )
    ctx_parser.add_argument("--project", default=None, help=argparse.SUPPRESS)
    subparsers.add_parser("_on_hook", help=argparse.SUPPRESS)

    # Groups management
    groups_parser = subparsers.add_parser("groups", help="Manage agent groups")
    groups_sub = groups_parser.add_subparsers(dest="groups_command")
    groups_sub.add_parser("list", help="List all groups")
    p_show = groups_sub.add_parser("show", help="Show group members")
    p_show.add_argument("name")
    p_create = groups_sub.add_parser("create", help="Create a new group")
    p_create.add_argument("name")
    p_create.add_argument("--description", default="")
    p_delete = groups_sub.add_parser("delete", help="Delete a group")
    p_delete.add_argument("name")
    p_join = groups_sub.add_parser("join", help="Add agent to group")
    p_join.add_argument("group")
    p_join.add_argument("agent")
    p_leave = groups_sub.add_parser("leave", help="Remove agent from group")
    p_leave.add_argument("group")
    p_leave.add_argument("agent")

    # Relay command (intra-group agent-to-agent, always waits for reply)
    relay_parser = subparsers.add_parser("relay", help="Send message between agents in the same group")
    relay_parser.add_argument("from_agent")
    relay_parser.add_argument("to_agent")
    _add_message_input_args(relay_parser)
    relay_parser.add_argument("--timeout", type=int, default=300)

    watch_parser = subparsers.add_parser("watch", help="Long-poll for hook events (no shell loop needed)")
    watch_parser.add_argument("--since", default=None, help="ISO-8601 timestamp; only return events after this time")
    watch_parser.add_argument("--timeout", type=int, default=30, help="Per-request timeout in seconds (default: 30)")
    watch_parser.add_argument("--loop", action="store_true", help="Continuously poll until Ctrl-C (default: one-shot)")
    watch_parser.add_argument("--format", choices=["json", "text"], default="json")

    # CDP provider subcommands
    cdp_parser = subparsers.add_parser("cdp", help="CDP provider management")
    cdp_sub = cdp_parser.add_subparsers(dest="cdp_command")
    cdp_sub.add_parser("status", help="Show CDP agent health").add_argument("agent")
    p_screenshot = cdp_sub.add_parser("screenshot", help="Take CDP screenshot")
    p_screenshot.add_argument("agent")
    p_screenshot.add_argument("--path", default="/tmp/ide_screenshot.png")
    cdp_sub.add_parser("models", help="List available models").add_argument("agent")
    p_switch = cdp_sub.add_parser("switch", help="Switch model")
    p_switch.add_argument("agent")
    p_switch.add_argument("model")
    cdp_sub.add_parser("probe", help="Run UI diagnostics").add_argument("agent")
    cdp_sub.add_parser("heal", help="Run auto-healer").add_argument("agent")

    return parser.parse_args(argv)


def _add_message_input_args(command_parser):
    command_parser.add_argument("message", nargs="?", help="Message body. Use --stdin or --message-file for multiline text.")
    source = command_parser.add_mutually_exclusive_group()
    source.add_argument("--stdin", action="store_true", help="Read the full message body from standard input.")
    source.add_argument("--message-file", help="Read the full message body from a UTF-8 text file.")


def _resolve_message_input(args):
    has_inline = getattr(args, "message", None) is not None
    has_stdin = bool(getattr(args, "stdin", False))
    message_file = getattr(args, "message_file", None)
    has_file = message_file is not None
    if sum([has_inline, has_stdin, has_file]) != 1:
        raise ValueError("provide exactly one message source: MESSAGE, --stdin, or --message-file")
    if has_stdin:
        return sys.stdin.read()
    if has_file:
        try:
            return Path(message_file).read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ValueError("cannot read --message-file %s: %s" % (message_file, exc)) from exc
    return args.message


def load_json(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def mutate_registry_groups(mutator_fn):
    """Atomically read registry.json, apply mutator_fn(groups), write back.

    Uses a platform lock to prevent concurrent mutations from losing updates.
    mutator_fn receives the groups dict and must modify it in-place.
    """
    lock_path = REGISTRY_PATH.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_fd:
        _lock_file(lock_fd)
        try:
            registry = load_json(REGISTRY_PATH)
            groups = registry.get("groups", {})
            mutator_fn(groups)
            registry["groups"] = groups
            tmp = REGISTRY_PATH.with_suffix(".tmp")
            write_json(tmp, registry)
            os.replace(str(tmp), str(REGISTRY_PATH))
        finally:
            _unlock_file(lock_fd)


def _lock_file(lock_fd):
    if _fcntl is not None:
        _fcntl.flock(lock_fd, _fcntl.LOCK_EX)
        return
    lock_fd.seek(0, os.SEEK_END)
    if lock_fd.tell() == 0:
        lock_fd.write("\0")
        lock_fd.flush()
    lock_fd.seek(0)
    _msvcrt.locking(lock_fd.fileno(), _msvcrt.LK_LOCK, 1)


def _unlock_file(lock_fd):
    if _fcntl is not None:
        _fcntl.flock(lock_fd, _fcntl.LOCK_UN)
        return
    lock_fd.seek(0)
    _msvcrt.locking(lock_fd.fileno(), _msvcrt.LK_UNLCK, 1)


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
    for session_file in sorted(sessions_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        stem = session_file.stem
        if "_" not in stem:
            continue
        agent_name = stem.rsplit("_", 1)[0]
        if agent_name not in session_map:
            session_map[agent_name] = active_session_key(session_file) or stem
    return session_map


def active_session_key(session_file):
    try:
        data = load_json(session_file)
    except (OSError, json.JSONDecodeError):
        return None
    active = data.get("active_session") or {}
    if not active:
        return None
    if len(active) == 1:
        return next(iter(active.keys()))

    sessions = data.get("sessions") or {}
    best_key = None
    best_timestamp = -1.0
    for session_key, session_id in active.items():
        session = sessions.get(session_id) or {}
        history = session.get("history") or []
        timestamp = 0.0
        if history:
            timestamp = _parse_event_timestamp(history[-1].get("timestamp"))
        if timestamp > best_timestamp:
            best_key = session_key
            best_timestamp = timestamp
    return best_key or next(iter(active.keys()))


def load_legacy_agent_config():
    if not LEGACY_CONFIG_PATH.exists():
        return {}
    data = load_json(LEGACY_CONFIG_PATH)
    return data.get("agents", {})


def bootstrap_registry_and_bindings():
    legacy_agents = load_legacy_agent_config()
    session_map = session_keys_by_agent()

    # Preserve existing groups from current registry
    existing_registry = {}
    if REGISTRY_PATH.exists():
        existing_registry = load_json(REGISTRY_PATH)

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
            legacy = legacy_agents.get(name, {})
            data_dir = config.get("data_dir", str(DATA_DIR))
            if isinstance(data_dir, str):
                data_dir = os.path.expanduser(data_dir)
            else:
                data_dir = str(DATA_DIR)
            registry["agents"][name] = {
                "type": agent_cfg.get("type", "unknown"),
                "provider": "cc_connect",
                "work_dir": options.get("work_dir", ""),
                "capabilities": ["message.send", "history.read", "session.control"],
                "labels": [],
            }
            session_key = session_map.get(name, "") or legacy.get("session_key", "")
            bindings["cc_connect"][name] = {
                "config_path": str(config_path),
                "data_dir": data_dir,
                "webhook_host": "127.0.0.1",
                "webhook_port": int(webhook.get("port", 0) or legacy.get("webhook_port", 0)),
                "webhook_path": webhook.get("path", "/hook") or legacy.get("webhook_path", "/hook"),
                "session_key": session_key,
                "platform": legacy.get("platform", "") or _platform_from_session_key(session_key),
            }

    # Preserve groups from existing registry
    existing_groups = existing_registry.get("groups", {})
    if existing_groups:
        registry["groups"] = existing_groups

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


def _build_agent_entry(registry, bindings, agent_name, strict_binding=False):
    """Build full agent dict or None if not found.

    If strict_binding=True, raises KeyError when agent exists but binding is missing.
    Used for exact-match lookups to surface configuration errors.
    """
    agent = registry.get("agents", {}).get(agent_name)
    if not agent:
        return None
    binding = bindings.get(agent["provider"], {}).get(agent_name)
    if not binding:
        if strict_binding:
            raise KeyError("Missing binding for agent: %s" % agent_name)
        return None
    combined = dict(agent)
    combined["name"] = agent_name
    combined["binding"] = binding
    return combined


def resolve_agent(registry, bindings, query, sender=None, group=None):
    """Resolve agent by exact name, then by type/prefix with same-group preference.

    1. Exact name match → return immediately (strict binding check).
    2. Otherwise find agents whose name contains <query> or whose type equals <query>.
    3. If `group` is given, filter candidates to that group first.
    4. Among candidates, prefer the one sharing a group with `sender`.
    5. If multiple same-group candidates remain, raise with disambiguation.
    6. If no candidates at all, raise KeyError.
    """
    # 1. Exact match (strict: raise if binding missing)
    exact = _build_agent_entry(registry, bindings, query, strict_binding=True)
    if exact:
        return exact

    # 2. Fuzzy: name contains query, or type matches query
    all_agents = registry.get("agents", {})
    candidates = []
    for name, info in all_agents.items():
        if query in name or info.get("type", "") == query:
            entry = _build_agent_entry(registry, bindings, name)
            if entry:
                candidates.append(entry)

    if not candidates:
        raise KeyError("Unknown agent: %s" % query)

    if len(candidates) == 1:
        return candidates[0]

    # 3. --group pre-filter: restrict candidates to the specified group
    if group:
        try:
            group_members = set(get_group_members(registry, group))
        except KeyError:
            raise KeyError("Unknown group: %s" % group)
        candidates = [c for c in candidates if c["name"] in group_members]
        if not candidates:
            raise KeyError("No agent matching '%s' in group '%s'" % (query, group))
        if len(candidates) == 1:
            return candidates[0]

    # 4. Same-group preference (exclude sender itself from candidates)
    if sender:
        candidates = [c for c in candidates if c["name"] != sender]
        if not candidates:
            raise KeyError("Unknown agent: %s" % query)
        if len(candidates) == 1:
            return candidates[0]
        sender_groups = set(get_agent_groups(registry, sender))
        if sender_groups:
            same_group = [c for c in candidates
                          if set(get_agent_groups(registry, c["name"])) & sender_groups]
            if len(same_group) == 1:
                return same_group[0]
            if len(same_group) > 1:
                raise KeyError(
                    "Multiple agents match '%s' in sender's group [%s]: %s. "
                    "Use exact name." % (
                        query, ", ".join(sender_groups),
                        ", ".join(c["name"] for c in same_group)))

    # 5. Multiple matches, no sender or no group overlap
    names = ", ".join(c["name"] for c in candidates)
    raise KeyError("Multiple agents match '%s': %s. Use exact name or --group." % (query, names))


def get_provider(agent):
    if agent["provider"] == "cc_connect":
        return CCConnectProvider(agent["name"], agent["binding"])
    if agent["provider"] == "cdp":
        from providers.cdp_provider import CDPProvider
        return CDPProvider(agent["name"], agent["binding"])
    raise ValueError("Unsupported provider: %s" % agent["provider"])


def get_groups(registry):
    return registry.get("groups", {})


def get_agent_groups(registry, agent_name):
    groups = get_groups(registry)
    return [name for name, g in groups.items() if agent_name in g.get("members", [])]


def get_group_members(registry, group_name):
    groups = get_groups(registry)
    group = groups.get(group_name)
    if not group:
        raise KeyError("Unknown group: %s" % group_name)
    return list(group.get("members", []))


def check_group_compatibility(registry, sender, target):
    sender_groups = set(get_agent_groups(registry, sender))
    target_groups = set(get_agent_groups(registry, target))
    if not sender_groups and not target_groups:
        return True
    if not sender_groups or not target_groups:
        return False
    return bool(sender_groups & target_groups)


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


def _resolve_cc_connect_command():
    if os.name == "nt":
        candidates = ["cc-connect.exe", "cc-connect.cmd", "cc-connect"]
    else:
        candidates = ["cc-connect", "cc-connect.cmd", "cc-connect.exe"]
    for candidate in candidates:
        path = shutil.which(candidate)
        if path:
            return path
    return "cc-connect"


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
        _resolve_cc_connect_command(),
        "--config",
        config_path,
        "send",
        "-p",
        origin_project,
        "-s",
        origin_session,
        "--stdin",
    ]
    try:
        result = runner(command, content)
    except FileNotFoundError as exc:
        return {"status": "skipped", "reason": "cc_connect_missing", "error": str(exc)}
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

    relay_reply = parse_relay_reply(content)
    if not relay_reply:
        store.append_event(
            event_type=event_type,
            agent_id=agent_id,
            request_id=None,
            session_key=session_key,
            content=content,
            timestamp=timestamp,
        )
        return {"status": "unmatched", "reason": "missing_relay_marker", "session_key": session_key}

    message = store.get_message(relay_reply["request_id"])
    if (
        not message
        or message.get("session_key") != session_key
        or message.get("target") != agent_id
        or message.get("status") not in ("pending", "delivered")
    ):
        store.append_event(
            event_type=event_type,
            agent_id=agent_id,
            request_id=relay_reply["request_id"],
            session_key=session_key,
            content=relay_reply["content"],
            timestamp=timestamp,
        )
        return {"status": "unmatched", "request_id": relay_reply["request_id"], "session_key": session_key}

    store.append_event(
        event_type=event_type,
        agent_id=agent_id,
        request_id=message["request_id"],
        session_key=session_key,
        content=relay_reply["content"],
        timestamp=timestamp,
    )

    store.mark_replied(message["request_id"], relay_reply["content"], timestamp)
    store.release_session_lock(session_key, message["request_id"])

    notify = notify_origin_reply(message, relay_reply["content"], bindings=bindings, runner=runner)
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
    filter_group = getattr(args, "group", None)
    agents = []
    for agent_name in sorted(registry.get("agents", {}).keys()):
        agent_groups = get_agent_groups(registry, agent_name)
        if filter_group and filter_group not in agent_groups:
            continue
        agent = get_agent(registry, bindings, agent_name)
        agents.append({
            "name": agent_name,
            "type": agent["type"],
            "provider": agent["provider"],
            "work_dir": agent["work_dir"],
            "webhook_port": agent["binding"].get("webhook_port", 0),
            "session_key": agent["binding"].get("session_key", ""),
            "groups": ", ".join(agent_groups) if agent_groups else "-",
        })

    if args.format == "json":
        print(json.dumps(agents, indent=2, ensure_ascii=False))
        return

    print("Found %d agent(s):" % len(agents))
    print("")
    print("  {0:<16} {1:<12} {2:<12} {3:<10} {4:<10} {5}".format(
        "Name", "Type", "Provider", "Group", "Webhook", "Session"
    ))
    print("  {0:<16} {1:<12} {2:<12} {3:<10} {4:<10} {5}".format(
        "─" * 16, "─" * 12, "─" * 12, "─" * 10, "─" * 10, "─" * 28
    ))
    for item in agents:
        webhook = ":%s" % item["webhook_port"] if item["webhook_port"] else "none"
        session_key = item["session_key"] or "none"
        print("  {0:<16} {1:<12} {2:<12} {3:<10} {4:<10} {5}".format(
            item["name"], item["type"], item["provider"], item["groups"], webhook, session_key
        ))


def cmd_info(args):
    registry, bindings = ensure_registry_and_bindings()
    sender = os.environ.get("CC_PROJECT") or ""
    try:
        agent = resolve_agent(registry, bindings, args.agent, sender=sender)
    except KeyError as e:
        print("Error: %s" % e)
        return 1
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


def cmd_bootstrap(args):
    registry, bindings = bootstrap_registry_and_bindings()
    agents = registry.get("agents", {})
    if not agents:
        print("No agents discovered. Check ~/.cc-connect/config*.toml files.")
        return 1

    print("Discovered %d agent(s). Verifying connectivity...\n" % len(agents))
    print("  {0:<16} {1:<10} {2:<10} {3:<10} {4}".format(
        "Agent", "Webhook", "Session", "Hook", "Status"
    ))
    print("  {0:<16} {1:<10} {2:<10} {3:<10} {4}".format(
        "─" * 16, "─" * 10, "─" * 10, "─" * 10, "─" * 12
    ))

    all_ok = True
    session_missing = []
    for agent_name in sorted(agents):
        agent = get_agent(registry, bindings, agent_name)
        provider = get_provider(agent)
        health = provider.get_health()
        hook_ok = "ok" in health.details and "hook=ok" in health.details
        session_ok = "session_file=" in health.details and "session_file=missing" not in health.details
        webhook_ok = health.provider_status == "up"

        webhook_str = "up" if webhook_ok else "down"
        session_str = "ok" if session_ok else "missing"
        hook_str = "ok" if hook_ok else "missing"
        status_str = "%s/%s" % (health.provider_status, health.agent_status)

        print("  {0:<16} {1:<10} {2:<10} {3:<10} {4}".format(
            agent_name, webhook_str, session_str, hook_str, status_str
        ))
        if not webhook_ok:
            all_ok = False
        if not session_ok:
            session_missing.append(agent_name)

    print()
    if session_missing:
        print("Session missing for: %s" % ", ".join(session_missing))
        print("  → Send a message to each bot via its chat platform (Feishu/Telegram/etc.),")
        print("    then rerun: cc-relay-hub bootstrap")
        print()

    # Auto-generate context files
    print("Generating agent context files...")
    ctx_args = argparse.Namespace(write=True, check=False, print_mode=False)
    cmd_bootstrap_context(ctx_args)

    if all_ok:
        print("\nAll agents reachable.")
        return 0
    else:
        print("\nSome agents are unreachable. Check that cc-connect is running.")
        return 1


# Agent context file adapters
# Each adapter: (filename, generator_fn(registry, bindings, agent_name) -> str or None)

def _hub_command():
    return str(HUB_BIN)


def _routing_contract_lines(agent_name=None):
    cmd = _hub_command()
    relay_from = agent_name or "<from-agent>"
    return [
        "## Message Routing Contract",
        "",
        "cc-connect relay and cc-relay-hub are different systems. Keep them separate.",
        "",
        "Use cc-relay-hub for direct agent-to-agent work, especially when the user says:",
        "- \"send a message to codex\"",
        "- \"ask Claude/Codex\"",
        "- \"let another agent continue\"",
        "- \"delegate this to <agent>\"",
        "- \"relay to <agent>\"",
        "",
        "Use these commands:",
        "```bash",
        "%s list --format json" % cmd,
        "%s info <agent>" % cmd,
        "%s send <agent> \"task\" --wait --timeout 120" % cmd,
        "Get-Content task.md -Raw | %s send <agent> --stdin --wait --timeout 120" % cmd,
        "%s relay %s <to-agent> \"task\" --timeout 120" % (cmd, relay_from),
        "```",
        "",
        "For multiline or long messages, use `--stdin` or `--message-file`; do not pass a PowerShell multiline variable as the quoted positional MESSAGE on Windows.",
        "Use cc-connect relay only for cc-connect's group-chat relay feature after a chat has been bound with /bind.",
        "Do not use `cc-connect relay send` for direct/private agent-to-agent delegation.",
        "Do not send messages across groups. Treat ungrouped agents as separate from named groups; exact agent names do not bypass group isolation.",
        "If the user says \"send to codex\" or \"ask codex\", default to cc-relay-hub.",
        "If you receive `[cc-relay request_id=...]`, that message is addressed to you. Never answer `NO_REPLY` or an empty response.",
        "Even if the task says \"only reply X\", put `X` after the required `[cc-relay reply_to=...]` marker.",
        "",
    ]


def _generate_agents_md(registry, bindings, agent_name):
    """Generate AGENTS.md content — universal source of truth."""
    lines = []

    # Dynamic header with agent info
    lines.append("# cc-relay-hub Agent Context\n")
    lines.append("You are connected to **cc-relay-hub**, a local multi-agent message router.\n")

    if agent_name:
        lines.append("Your agent name: **%s**\n" % agent_name)

    # Groups
    agent_groups = get_agent_groups(registry, agent_name) if agent_name else []
    if agent_groups:
        lines.append("Your groups: %s\n" % ", ".join(agent_groups))

    # Peers (dynamic, from registry)
    lines.append("\n## Available Peers\n")
    lines.append("```")
    for name in sorted(registry.get("agents", {}).keys()):
        if name == agent_name:
            continue
        a = registry["agents"][name]
        a_groups = get_agent_groups(registry, name)
        group_str = " [%s]" % ",".join(a_groups) if a_groups else ""
        lines.append("  %-16s %s%s" % (name, a.get("type", "?"), group_str))
    lines.append("```\n")

    lines.extend(_routing_contract_lines(agent_name))

    # Static body from template (NOT from generated output)
    template = HUB_DIR / "templates" / "AGENTS.md"
    if template.exists():
        content = template.read_text(encoding="utf-8")
        # Skip the first heading (we already wrote our own)
        in_body = False
        for line in content.split("\n"):
            if in_body:
                lines.append(line)
            elif line.startswith("## "):
                in_body = True
                lines.append("\n" + line)

    return "\n".join(lines)


def _generate_claude_md(registry, bindings, agent_name):
    """Generate CLAUDE.md for Claude Code agents."""
    lines = []
    lines.append("# cc-relay-hub\n")
    lines.append("Multi-agent message router. Use `cc-relay-hub` to discover peers and send messages.\n")
    lines.append("## Quick Reference\n")
    lines.append("```bash")
    lines.append("cc-relay-hub list")
    lines.append("cc-relay-hub info <agent>")
    lines.append("cc-relay-hub send <agent> \"message\" --wait --timeout 120")
    lines.append("Get-Content task.md -Raw | cc-relay-hub send <agent> --stdin --wait --timeout 120")
    lines.append("cc-relay-hub cdp status <cdp-agent>")
    lines.append("cc-relay-hub groups")
    lines.append("cc-relay-hub relay <from> <to> \"message\"")
    lines.append("```\n")

    if agent_name:
        lines.append("You are: **%s**\n" % agent_name)

    lines.extend(_routing_contract_lines(agent_name))

    # Peers
    lines.append("## Peers\n")
    for name in sorted(registry.get("agents", {}).keys()):
        if name == agent_name:
            continue
        a = registry["agents"][name]
        provider = a.get("provider", "?")
        lines.append("- **%s** (%s, provider=%s)" % (name, a.get("type", "?"), provider))

    cdp_agents = [
        name for name, info in sorted(registry.get("agents", {}).items())
        if info.get("provider") == "cdp" and name != agent_name
    ]
    if cdp_agents:
        lines.append("\n## CDP IDE Agents\n")
        lines.append("CDP-backed IDE agents are still contacted with `send --wait`; the hub writes into the IDE Agent chat through localhost CDP and reads the visible transcript.")
        lines.append("")
        lines.append("```bash")
        lines.append("cc-relay-hub info %s" % cdp_agents[0])
        lines.append("cc-relay-hub cdp status %s" % cdp_agents[0])
        lines.append("cc-relay-hub send %s \"task\" --wait --timeout 120" % cdp_agents[0])
        lines.append("```")
        lines.append("")
        lines.append("- Empty `Session` and `Last Seen: never` are normal for CDP agents.")
        lines.append("- If a CDP send times out, run `cc-relay-hub cdp probe <agent>`, `cc-relay-hub cdp heal <agent>`, and `cc-relay-hub cdp screenshot <agent> --path /tmp/ide.png` before retrying.")
        lines.append("- Do not use `cc-connect relay send` for Antigravity/CDP delegation.")

    # Key facts
    lines.append("\n## Key Facts\n")
    lines.append("- `send` is provider-aware: cc-connect agents use local webhook HTTP POST; CDP agents use localhost Chrome DevTools Protocol.")
    lines.append("- Use `--stdin` or `--message-file` for multiline or long tasks, especially on Windows.")
    lines.append("- CDP agents use virtual session key `cdp:<agent_name>`.")
    lines.append("- Hook-server long-poll: `GET http://127.0.0.1:9120/events/longpoll`")
    lines.append("- When receiving relay markers, reply with the same marker.")
    return "\n".join(lines)


def _generate_gemini_md(registry, bindings, agent_name):
    """Generate GEMINI.md for Gemini CLI agents."""
    return _generate_claude_md(registry, bindings, agent_name).replace(
        "# cc-relay-hub", "# cc-relay-hub (Gemini)"
    )


def _generate_cursor_rules(registry, bindings, agent_name):
    """Generate .cursorrules for Cursor agents."""
    lines = []
    lines.append("# cc-relay-hub")
    lines.append("")
    lines.append("You are connected to cc-relay-hub, a local multi-agent message router.")
    if agent_name:
        lines.append("Your agent name: %s" % agent_name)
    lines.append("")
    lines.extend(_routing_contract_lines(agent_name))
    lines.append("")
    lines.append("## Commands")
    lines.append("- List agents: `%s list --format json`" % _hub_command())
    lines.append("- Send message: `%s send <agent> \"msg\" --wait`" % _hub_command())
    lines.append("- Send multiline message: `Get-Content task.md -Raw | %s send <agent> --stdin --wait`" % _hub_command())
    lines.append("- Check health: `%s info <agent>`" % _hub_command())
    lines.append("")
    lines.append("## Rules")
    lines.append("- Never hardcode agent names. Discover with `%s list --format json`." % _hub_command())
    lines.append("- Check agent health before sending work.")
    lines.append("- When receiving relay markers, reply with the same marker.")
    return "\n".join(lines)


# (filename, generator, per_agent)
# per_agent=False: generated once, universal (e.g. AGENTS.md)
# per_agent=True: generated for each agent (e.g. CLAUDE.md)
_CONTEXT_ADAPTERS = [
    ("AGENTS.md", _generate_agents_md, False),
    ("CLAUDE.md", _generate_claude_md, True),
    ("GEMINI.md", _generate_gemini_md, True),
    (".cursorrules", _generate_cursor_rules, True),
]


CC_RELAY_CONTEXT_START = "<!-- cc-relay-hub:begin -->"
CC_RELAY_CONTEXT_END = "<!-- cc-relay-hub:end -->"


def _context_block(content):
    return "\n%s\n%s\n%s\n" % (
        CC_RELAY_CONTEXT_START,
        content.strip(),
        CC_RELAY_CONTEXT_END,
    )


def _merge_context_block(existing, content):
    block = _context_block(content)
    start = existing.find(CC_RELAY_CONTEXT_START)
    if start >= 0:
        end = existing.find(CC_RELAY_CONTEXT_END, start)
        if end >= 0:
            end += len(CC_RELAY_CONTEXT_END)
            prefix = existing[:start].rstrip()
            suffix = existing[end:].lstrip("\n")
            if prefix and suffix:
                return prefix + block + suffix
            if prefix:
                return prefix + block
            if suffix:
                return block.lstrip("\n") + suffix
            return block.lstrip("\n")
        prefix = existing[:start].rstrip()
        return (prefix + block if prefix else block.lstrip("\n"))

    stripped = existing.lstrip()
    if stripped.startswith("# cc-relay-hub Agent Context") or stripped.startswith("# cc-relay-hub\n"):
        return block.lstrip("\n")

    if existing.strip():
        return existing.rstrip() + block
    return block.lstrip("\n")


def _agent_memory_spec(agent_type):
    """Return (project filename, global memory path) mirroring cc-connect agents."""
    agent_type = (agent_type or "").lower()
    home = Path.home()
    if agent_type in ("claudecode", "claude"):
        return "CLAUDE.md", home / ".claude" / "CLAUDE.md"
    if agent_type == "codex":
        codex_home = Path(os.environ.get("CODEX_HOME") or (home / ".codex"))
        return "AGENTS.md", codex_home / "AGENTS.md"
    if agent_type == "gemini":
        return "GEMINI.md", home / ".gemini" / "GEMINI.md"
    if agent_type == "opencode":
        return "OPENCODE.md", home / ".opencode" / "OPENCODE.md"
    if agent_type == "iflow":
        return "IFLOW.md", home / ".iflow" / "IFLOW.md"
    if agent_type == "kimi":
        return "AGENTS.md", home / ".kimi" / "AGENTS.md"
    if agent_type == "qoder":
        return "AGENTS.md", home / ".qoder" / "AGENTS.md"
    if agent_type == "pi":
        return "AGENTS.md", home / ".pi" / "AGENTS.md"
    if agent_type == "cursor":
        return ".cursorrules", None
    return "AGENTS.md", None


def _read_json_file(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _cc_connect_data_dir(bindings, agent_name):
    binding = bindings.get("cc_connect", {}).get(agent_name, {})
    data_dir = binding.get("data_dir") or str(DATA_DIR)
    return Path(os.path.expanduser(data_dir))


def _cc_connect_project_dirs(registry, bindings, agent_name):
    agent = registry.get("agents", {}).get(agent_name, {})
    dirs = []

    def add_dir(value):
        if not value:
            return
        path = Path(os.path.expanduser(str(value)))
        if not path.is_absolute():
            path = path.resolve()
        if path.exists() and path.is_dir():
            clean = str(path)
            if clean not in dirs:
                dirs.append(clean)

    add_dir(agent.get("work_dir"))

    data_dir = _cc_connect_data_dir(bindings, agent_name)
    state = _read_json_file(data_dir / "projects" / ("%s.state.json" % agent_name), {})
    add_dir(state.get("work_dir_override"))
    for value in (state.get("workspace_dir_overrides") or {}).values():
        add_dir(value)

    history = _read_json_file(data_dir / "dir_history.json", {})
    for value in history.get(agent_name, []) or []:
        add_dir(value)

    return dirs


def _context_target_records(registry, bindings, scope, project_filter=None):
    agents = registry.get("agents", {})
    records = []
    seen = set()

    def add_record(path, generator, agent_name, label):
        if not path:
            return
        key = (str(path), agent_name or "", label)
        if key in seen:
            return
        seen.add(key)
        records.append({
            "path": Path(path),
            "generator": generator,
            "agent_name": agent_name,
            "label": label,
        })

    selected = sorted(agents.keys())
    if project_filter:
        selected = [name for name in selected if name == project_filter]

    if scope in ("cwd",):
        project_dir = Path.cwd()
        for filename, generator, per_agent in _CONTEXT_ADAPTERS:
            agent_names = selected if per_agent else [None]
            for agent_name in agent_names:
                if not per_agent:
                    out_path = project_dir / filename
                elif len(agents) > 1:
                    out_path = project_dir / ".cc-relay-hub" / agent_name / filename
                else:
                    out_path = project_dir / filename
                add_record(out_path, generator, agent_name, "cwd")

    if scope in ("global", "all"):
        global_paths = {}
        for agent_name in selected:
            agent = agents.get(agent_name, {})
            _, global_path = _agent_memory_spec(agent.get("type", ""))
            if not global_path:
                continue
            # Global memory files are shared by all agents of the same CLI.
            # Do not stamp a specific agent identity into shared memory.
            global_paths[str(global_path)] = global_path
        for global_path in global_paths.values():
            add_record(global_path, _generate_agents_md, None, "global")

    if scope in ("workdirs", "all"):
        by_path = {}
        for agent_name in selected:
            agent = agents.get(agent_name, {})
            filename, _ = _agent_memory_spec(agent.get("type", ""))
            for work_dir in _cc_connect_project_dirs(registry, bindings, agent_name):
                path = Path(work_dir) / filename
                by_path.setdefault(str(path), {"path": path, "agents": []})["agents"].append(agent_name)

        for item in by_path.values():
            agent_names = item["agents"]
            agent_name = agent_names[0] if len(agent_names) == 1 else None
            generator = _generate_cursor_rules if item["path"].name == ".cursorrules" else _generate_agents_md
            add_record(item["path"], generator, agent_name, "workdir")

    return records


def cmd_bootstrap_context(args):
    registry, bindings = ensure_registry_and_bindings()
    do_write = getattr(args, "write", False)
    do_check = getattr(args, "check", False)
    do_print = getattr(args, "print_mode", False)
    scope = getattr(args, "scope", "all") or "all"
    project_filter = getattr(args, "project", None)

    if not do_write and not do_check and not do_print:
        do_print = True

    results = []

    for record in _context_target_records(registry, bindings, scope, project_filter):
        out_path = record["path"]
        content = record["generator"](registry, bindings, record["agent_name"])
        if content is None:
            continue

        if do_print:
            print("=== %s (%s) ===" % (out_path, record["label"]))
            print(_merge_context_block("", content).rstrip())
            print()
            continue

        if do_check:
            exists = out_path.exists()
            current = out_path.read_text(encoding="utf-8") if exists else ""
            desired = _merge_context_block(current, content)
            up_to_date = exists and current == desired
            status = "ok" if up_to_date else ("missing" if not exists else "outdated")
            results.append((str(out_path), status))
            continue

        if do_write:
            current = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
            desired = _merge_context_block(current, content)
            if out_path.exists() and current != desired:
                backup = out_path.with_suffix(out_path.suffix + ".bak")
                backup.write_text(current, encoding="utf-8")

            if current != desired:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(desired, encoding="utf-8")
                status = "written"
            else:
                status = "ok"
            results.append((str(out_path), status))

    if do_check:
        print("Context file status:")
        for path, status in results:
            marker = {"ok": "  ", "missing": "!!", "outdated": "~ "}.get(status, "??")
            print("  [%s] %s" % (marker, path))
        outdated = [r for r in results if r[1] != "ok"]
        if outdated:
            print("\n%d file(s) need update. Run: cc-relay-hub bootstrap context --write" % len(outdated))
            return 1
        print("\nAll context files up to date.")
        return 0

    if do_write:
        for path, status in results:
            print("  %s: %s" % (status, path))
        return 0

    return 0


def cmd_send(args):
    try:
        message_body = _resolve_message_input(args)
    except ValueError as exc:
        print("Error: %s" % exc)
        return 1

    registry, bindings = ensure_registry_and_bindings()

    origin_project, origin_session = resolve_origin_context(args)
    sender = origin_project or os.environ.get("CC_PROJECT") or ""
    filter_group = getattr(args, "group", None)

    # Resolve agent with same-group preference; --group participates in disambiguation
    try:
        agent = resolve_agent(registry, bindings, args.agent, sender=sender, group=filter_group)
    except KeyError as e:
        print("Error: %s" % e)
        return 1

    # Validate resolved agent is in the specified group (catches exact-match bypass)
    if filter_group:
        try:
            members = get_group_members(registry, filter_group)
        except KeyError:
            print("Error: unknown group '%s'" % filter_group)
            return 1
        if agent["name"] not in members:
            print("Error: agent '%s' is not in group '%s'. Members: %s" % (
                agent["name"], filter_group, ", ".join(members)))
            return 1

    # Group isolation: reject sends across group boundaries. Ungrouped agents
    # can talk to other ungrouped agents, but not to named groups.
    if sender and not filter_group:
        if not check_group_compatibility(registry, sender, agent["name"]):
            sender_g = get_agent_groups(registry, sender)
            target_g = get_agent_groups(registry, agent["name"])
            print("Error: cross-group send blocked (%s in [%s] -> %s in [%s])" % (
                sender, ", ".join(sender_g), agent["name"], ", ".join(target_g)))
            return 1

    provider = get_provider(agent)
    state = StateStore(str(STATE_DB_PATH))
    session_key = agent["binding"].get("session_key", "")

    if agent["provider"] != "cdp" and not session_key:
        print("Error: agent %s has no session_key yet. Chat with the bot once first." % agent["name"])
        return 1

    effective_session = session_key or "cdp:%s" % agent["name"]
    request_id = uuid.uuid4().hex
    now = time.time()
    envelope = RelayEnvelope(
        request_id=request_id,
        sender="cc-relay",
        target=agent["name"],
        body=message_body,
        created_at=now,
        reply_to=None,
        ttl=int(args.timeout),
    )
    state.insert_message(
        request_id=request_id,
        sender=envelope.sender,
        target=envelope.target,
        session_key=effective_session,
        provider=agent["provider"],
        body=envelope.body,
        status="pending",
        created_at=now,
        origin_project=origin_project,
        origin_session_key=origin_session,
    )

    locked = state.acquire_session_lock(effective_session, request_id, args.timeout)
    if not locked:
        state.mark_failed(request_id, "busy")
        print("busy: session %s already has a pending request" % effective_session)
        return 1

    is_control = message_body.startswith("/") and provider.supports_control()
    if is_control:
        result = provider.execute_command(message_body)
        if result.status != "delivered":
            state.mark_failed(request_id, result.error or "command failed")
            state.release_session_lock(effective_session, request_id)
            print("Error: %s" % (result.error or "command failed"))
            return 1
        state.mark_delivered(request_id, result.executed_at)
    else:
        receipt = provider.deliver(envelope)
        if receipt.status != "delivered":
            state.mark_failed(request_id, receipt.error or "delivery failed")
            state.release_session_lock(effective_session, request_id)
            print("Error: %s" % (receipt.error or "delivery failed"))
            return 1
        state.mark_delivered(request_id, receipt.delivered_at)

    if not args.wait:
        print("Message sent to %s (request_id=%s body_chars=%d)" % (
            agent["name"],
            request_id,
            len(envelope.body),
        ))
        return 0

    reply = wait_for_reply_framework(
        store=state,
        provider=provider,
        request_id=request_id,
        session_key=effective_session,
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


def cmd_groups(args):
    registry, bindings = ensure_registry_and_bindings()
    groups = get_groups(registry)
    subcmd = getattr(args, "groups_command", None) or "list"

    if subcmd == "list":
        if not groups:
            print("No groups defined. Use 'cc-relay groups create <name>' to create one.")
            return 0
        for name, group in sorted(groups.items()):
            desc = group.get("description", "")
            members = group.get("members", [])
            label = " (%s)" % desc if desc else ""
            print("  %s%s — %d member(s): %s" % (name, label, len(members), ", ".join(members)))
        return 0

    if subcmd == "show":
        group = groups.get(args.name)
        if not group:
            print("Error: unknown group '%s'" % args.name)
            return 1
        desc = group.get("description", "")
        members = group.get("members", [])
        print("Group: %s%s" % (args.name, " (%s)" % desc if desc else ""))
        print("  Members:")
        for m in members:
            try:
                agent = get_agent(registry, bindings, m)
                agent_groups = get_agent_groups(registry, m)
                try:
                    health = format_status(agent)
                    status = "%s (%s)" % (health["provider_status"], health["agent_status"])
                except Exception:
                    status = "error"
                print("    %-16s %-12s %-12s %s" % (m, agent["type"], agent["provider"], status))
            except KeyError:
                print("    %-16s (not in registry)" % m)
        return 0

    if subcmd == "create":
        def _create(g):
            if args.name in g:
                raise ValueError("already exists")
            g[args.name] = {"description": args.description, "members": []}
        try:
            mutate_registry_groups(_create)
        except ValueError:
            print("Error: group '%s' already exists" % args.name)
            return 1
        print("Created group '%s'" % args.name)
        return 0

    if subcmd == "delete":
        def _delete(g):
            if args.name not in g:
                raise ValueError("not found")
            del g[args.name]
        try:
            mutate_registry_groups(_delete)
        except ValueError:
            print("Error: unknown group '%s'" % args.name)
            return 1
        print("Deleted group '%s'" % args.name)
        return 0

    if subcmd == "join":
        registry_ro = load_json(REGISTRY_PATH)
        if args.agent not in registry_ro.get("agents", {}):
            print("Error: unknown agent '%s'" % args.agent)
            return 1
        result = []
        def _join(g):
            group = g.get(args.group)
            if not group:
                raise ValueError("group not found")
            members = group.get("members", [])
            if args.agent in members:
                result.append("already")
                return
            members.append(args.agent)
            group["members"] = members
        try:
            mutate_registry_groups(_join)
        except ValueError:
            print("Error: unknown group '%s'" % args.group)
            return 1
        if result and result[0] == "already":
            print("Agent '%s' is already in group '%s'" % (args.agent, args.group))
        else:
            print("Added '%s' to group '%s'" % (args.agent, args.group))
        return 0

    if subcmd == "leave":
        result = []
        def _leave(g):
            group = g.get(args.group)
            if not group:
                raise ValueError("group not found")
            members = group.get("members", [])
            if args.agent not in members:
                result.append("not_member")
                return
            members.remove(args.agent)
            group["members"] = members
        try:
            mutate_registry_groups(_leave)
        except ValueError:
            print("Error: unknown group '%s'" % args.group)
            return 1
        if result and result[0] == "not_member":
            print("Agent '%s' is not in group '%s'" % (args.agent, args.group))
        else:
            print("Removed '%s' from group '%s'" % (args.agent, args.group))
        return 0

    print("Usage: cc-relay groups <list|show|create|delete|join|leave>")
    return 1


def cmd_relay(args):
    try:
        message_body = _resolve_message_input(args)
    except ValueError as exc:
        print("Error: %s" % exc)
        return 1

    registry, bindings = ensure_registry_and_bindings()

    # Validate both agents exist
    try:
        from_agent = resolve_agent(registry, bindings, args.from_agent)
    except KeyError as e:
        print("Error: %s" % e)
        return 1
    try:
        to_agent = resolve_agent(registry, bindings, args.to_agent, sender=from_agent["name"])
    except KeyError as e:
        print("Error: %s" % e)
        return 1

    # Normalize names to resolved values
    from_name = from_agent["name"]
    to_name = to_agent["name"]

    # Check same group. Ungrouped agents can talk to other ungrouped agents,
    # but not to agents in a named group.
    from_groups = set(get_agent_groups(registry, from_name))
    to_groups = set(get_agent_groups(registry, to_name))
    if not check_group_compatibility(registry, from_name, to_name):
        print("Error: '%s' and '%s' are not in the same group." % (from_name, to_name))
        print("  %s groups: %s" % (from_name, ", ".join(from_groups) or "-"))
        print("  %s groups: %s" % (to_name, ", ".join(to_groups) or "-"))
        return 1

    # Send message from from_agent's context to to_agent
    to_session = to_agent["binding"].get("session_key", "")
    if to_agent["provider"] != "cdp" and not to_session:
        print("Error: agent '%s' has no session_key yet." % to_name)
        return 1

    from_session = from_agent["binding"].get("session_key", "")
    from_project = from_agent["binding"].get("session_key", "").split(":")[0] if from_session else from_name

    state = StateStore(str(STATE_DB_PATH))
    effective_session = to_session or "cdp:%s" % to_name
    request_id = uuid.uuid4().hex
    now = time.time()

    envelope = RelayEnvelope(
        request_id=request_id,
        sender=from_name,
        target=to_name,
        body=message_body,
        created_at=now,
        reply_to=None,
        ttl=int(args.timeout),
    )
    state.insert_message(
        request_id=request_id,
        sender=from_name,
        target=to_name,
        session_key=effective_session,
        provider=to_agent["provider"],
        body=envelope.body,
        status="pending",
        created_at=now,
        origin_project=from_name,
        origin_session_key=from_session,
    )

    locked = state.acquire_session_lock(effective_session, request_id, args.timeout)
    if not locked:
        state.mark_failed(request_id, "busy")
        print("busy: agent '%s' already has a pending request" % to_name)
        return 1

    provider = get_provider(to_agent)
    receipt = provider.deliver(envelope)
    if receipt.status != "delivered":
        state.mark_failed(request_id, receipt.error or "delivery failed")
        state.release_session_lock(effective_session, request_id)
        print("Error: %s" % (receipt.error or "delivery failed"))
        return 1
    state.mark_delivered(request_id, receipt.delivered_at)
    print("[%s -> %s] Message sent (request_id=%s body_chars=%d). Waiting for reply..." % (
        from_name,
        to_name,
        request_id,
        len(envelope.body),
    ))

    reply = wait_for_reply_framework(
        store=state,
        provider=provider,
        request_id=request_id,
        session_key=effective_session,
        timeout_secs=args.timeout,
        poll_interval=1.0,
    )
    if reply is None:
        print("timeout: no reply from '%s' within %ds" % (to_name, args.timeout))
        return 1

    print("[%s -> %s] Reply:" % (to_name, from_name))
    print(reply)

    # Notify the from-agent about the reply (only for cc_connect with valid session)
    if from_agent["provider"] == "cc_connect" and from_session:
        notify = notify_origin_reply(
            {"origin_project": from_name, "origin_session_key": from_session, "target": to_name},
            reply, bindings=bindings,
        )
        if notify["status"] == "sent":
            state.mark_notified(request_id, time.time())
        elif notify["status"] == "failed":
            state.mark_notify_failed(request_id, notify.get("error", "unknown"))
            print("Warning: reply notification to '%s' failed: %s" % (from_name, notify.get("error", "")))
    else:
        print("Note: reply not forwarded to '%s' (provider=%s, no session)" % (
            from_name, from_agent["provider"]))
    return 0


def cmd_cdp(args):
    """Dispatch CDP provider subcommands."""
    cdp_cmd = args.cdp_command
    if not cdp_cmd:
        print("Usage: cc-relay cdp <status|screenshot|models|switch|probe|heal> <agent>")
        return 1

    registry, bindings = ensure_registry_and_bindings()
    try:
        agent = get_agent(registry, bindings, args.agent)
    except KeyError as exc:
        print("Error: %s" % exc)
        return 1

    if agent.get("provider") != "cdp":
        print("Error: agent '%s' is not a CDP provider (provider=%s)" % (
            args.agent, agent.get("provider")))
        return 1

    provider = get_provider(agent)

    if cdp_cmd == "status":
        health = provider.get_health()
        print("Agent: %s" % args.agent)
        print("Provider: %s" % health.provider_status)
        print("Agent status: %s" % health.agent_status)
        print("Details: %s" % health.details)
        return 0 if health.provider_status == "up" else 1

    # Commands that need the backend connected
    try:
        backend = provider._ensure_backend()
    except Exception as exc:
        print("Error connecting to CDP: %s" % exc)
        return 1

    if cdp_cmd == "screenshot":
        path = getattr(args, "path", "/tmp/ide_screenshot.png")
        saved = backend.take_screenshot(path)
        print("Screenshot saved: %s" % saved)
        return 0

    if cdp_cmd == "models":
        if not hasattr(backend, "list_models"):
            print("Model listing not supported for %s" % backend.name)
            return 1
        models = backend.list_models()
        print(json.dumps(models, ensure_ascii=False, indent=2))
        return 0

    if cdp_cmd == "switch":
        if not hasattr(backend, "switch_model"):
            print("Model switching not supported for %s" % backend.name)
            return 1
        result = backend.switch_model(args.model)
        print(result)
        return 0 if result == "SWITCHED" else 1

    if cdp_cmd == "probe":
        from cdp.auto_heal import UIProbe
        backend.enable_auto_heal()
        conn = backend.ensure_conn()
        probe = UIProbe(conn)
        dom = probe.dump_dom_snapshot()
        print("DOM snapshot (%d chars):" % len(dom))
        print(dom[:3000])
        return 0

    if cdp_cmd == "heal":
        backend.enable_auto_heal()
        if not backend._healer:
            print("Auto-heal not available")
            return 1
        report = backend._healer.heal_chat_input()
        print("Target: %s" % report.target)
        print("Success: %s" % report.success)
        if report.new_selector:
            print("Selector: %s" % report.new_selector)
        if report.error:
            print("Error: %s" % report.error)
        return 0 if report.success else 1

    print("Unknown cdp command: %s" % cdp_cmd)
    return 1


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    if not args.command:
        print("Usage: cc-relay <list|send|info|groups|relay|cdp> ...")
        return 1
    if args.command == "list":
        cmd_list(args)
        return 0
    if args.command == "bootstrap":
        if getattr(args, "bootstrap_command", None) == "context":
            return cmd_bootstrap_context(args)
        return cmd_bootstrap(args)
    if args.command == "send":
        return cmd_send(args)
    if args.command == "info":
        cmd_info(args)
        return 0
    if args.command == "_on_hook":
        return cmd_on_hook(args)
    if args.command == "watch":
        return cmd_watch(args)
    if args.command == "groups":
        return cmd_groups(args)
    if args.command == "relay":
        return cmd_relay(args)
    if args.command == "cdp":
        return cmd_cdp(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
