import glob
import json
import os
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from core.envelope import CommandResult, DeliveryReceipt, ProviderHealth, RelayEvent
from core.relay_protocol import build_relay_prompt
from providers.base import ControlProvider, MessageProvider


class CCConnectProvider(MessageProvider, ControlProvider):
    def __init__(self, agent_id, binding):
        self.agent_id = agent_id
        self.binding = dict(binding)

    def deliver(self, envelope):
        delivered_at = time.time()
        payload = {
            "project": self.agent_id,
            "session_key": self.binding["session_key"],
            "platform": self.binding.get("platform") or _platform_from_session_key(self.binding["session_key"]),
            "prompt": build_relay_prompt(envelope),
            "event": "cc-relay",
        }
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self._webhook_url(),
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10):
                return DeliveryReceipt(
                    request_id=envelope.request_id,
                    status="delivered",
                    provider="cc_connect",
                    delivered_at=delivered_at,
                )
        except urllib.error.HTTPError as err:
            return DeliveryReceipt(
                request_id=envelope.request_id,
                status="failed",
                provider="cc_connect",
                delivered_at=delivered_at,
                error="HTTP %s" % err.code,
            )
        except urllib.error.URLError as err:
            return DeliveryReceipt(
                request_id=envelope.request_id,
                status="failed",
                provider="cc_connect",
                delivered_at=delivered_at,
                error=str(err.reason),
            )

    def poll_events(self, cursor=None):
        session_file = self._session_file()
        if not session_file or not session_file.exists():
            return []

        with session_file.open("r", encoding="utf-8") as handle:
            data = json.load(handle)

        lower_bound = 0.0
        if cursor:
            try:
                lower_bound = float(cursor)
            except ValueError:
                lower_bound = 0.0

        events = []
        sessions = data.get("sessions", {})
        for session in sessions.values():
            if not session:
                continue
            for item in (session.get("history") or []):
                if item.get("role") != "assistant":
                    continue
                timestamp = _parse_timestamp(item.get("timestamp", ""))
                if timestamp <= lower_bound:
                    continue
                events.append(
                    RelayEvent(
                        event_type="message.reply",
                        request_id=None,
                        agent_id=self.agent_id,
                        content=item.get("content", ""),
                        timestamp=timestamp,
                        session_key=self.binding.get("session_key"),
                    )
                )
        events.sort(key=lambda event: event.timestamp)
        return events

    def get_health(self):
        webhook_up = _tcp_connectable("127.0.0.1", int(self.binding.get("webhook_port", 0)))
        session_file = self._session_file()
        last_seen = None
        agent_status = "unresponsive"
        if session_file and session_file.exists():
            last_seen = _latest_history_timestamp(session_file)
            if last_seen is not None:
                agent_status = "busy" if (time.time() - last_seen) <= 60 else "idle"

        hook_configured = self._has_required_hook()
        details = "hook=%s, session_file=%s" % (
            "ok" if hook_configured else "missing",
            str(session_file) if session_file else "missing",
        )
        return ProviderHealth(
            provider_status="up" if webhook_up else "down",
            agent_status=agent_status,
            last_seen_at=last_seen,
            last_delivery_at=None,
            details=details,
        )

    def supports_control(self):
        return True

    def execute_command(self, command):
        payload = {
            "project": self.agent_id,
            "session_key": self.binding["session_key"],
            "platform": self.binding.get("platform") or _platform_from_session_key(self.binding["session_key"]),
            "prompt": command,
            "event": "cc-relay",
        }
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self._webhook_url(),
            data=data,
            headers={"Content-Type": "application/json"},
        )
        executed_at = time.time()
        try:
            with urllib.request.urlopen(request, timeout=10):
                return CommandResult(status="delivered", executed_at=executed_at)
        except urllib.error.HTTPError as err:
            return CommandResult(status="failed", executed_at=executed_at, error="HTTP %s" % err.code)
        except urllib.error.URLError as err:
            return CommandResult(status="failed", executed_at=executed_at, error=str(err.reason))

    def _webhook_url(self):
        host = self.binding.get("webhook_host", "127.0.0.1")
        port = int(self.binding["webhook_port"])
        path = self.binding.get("webhook_path", "/hook")
        return "http://%s:%d%s" % (host, port, path)

    def _session_file(self):
        matches = glob.glob(str(Path.home() / ".cc-connect" / "sessions" / ("%s_*.json" % self.agent_id)))
        if not matches:
            return None
        # An agent accumulates several session files as it is re-initialized over
        # time (codex-bot_<hash>.json). glob order is arbitrary, so matches[0] can
        # be a stale, dead session — making the hub poll the wrong file and miss
        # replies entirely (a --wait times out even though the agent answered
        # correctly with the reply marker). Pick the most recently modified file,
        # which is the active session currently being written to.
        newest = max(matches, key=os.path.getmtime)
        return Path(newest)

    def _has_required_hook(self):
        config_path = self.binding.get("config_path")
        if not config_path:
            return False
        config_file = Path(config_path)
        if not config_file.exists():
            return False
        with config_file.open("rb") as handle:
            config = tomllib.load(handle)
        for hook in config.get("hooks", []):
            if hook.get("event") != "message.sent":
                continue
            if hook.get("type") != "http":
                continue
            if hook.get("url") == "http://127.0.0.1:9120/cc-connect/hooks/reply":
                return True
        return False


def _parse_timestamp(value):
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        normalized = value
        if "." in value:
            head, tail = value.split(".", 1)
            frac = tail
            zone = ""
            for marker in ("+", "-", "Z"):
                idx = tail.find(marker)
                if idx > -1:
                    frac = tail[:idx]
                    zone = tail[idx:]
                    break
            frac = (frac + "000000")[:6]
            normalized = "%s.%s%s" % (head, frac, zone)
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        return datetime.strptime(normalized, "%Y-%m-%dT%H:%M:%S.%f%z").timestamp()


def _platform_from_session_key(session_key):
    if not session_key or ":" not in session_key:
        return ""
    return session_key.split(":", 1)[0]


def _latest_history_timestamp(session_file):
    with session_file.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    latest = None
    for session in data.get("sessions", {}).values():
        if not session:
            continue
        for item in (session.get("history") or []):
            timestamp = _parse_timestamp(item.get("timestamp", ""))
            latest = timestamp if latest is None else max(latest, timestamp)
    return latest


def _tcp_connectable(host, port):
    if not port:
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()
