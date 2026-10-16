"""CDP provider — adapts ElectronCDPBackend to the MessageProvider interface."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request

from core.envelope import DeliveryReceipt, ProviderHealth, RelayEvent
from core.relay_protocol import build_relay_prompt, extract_reply_from_transcript
from providers.base import ControlProvider, MessageProvider


_CDP_MARKER_RE = re.compile(r"\[cc-relay\s+reply_to=([A-Za-z0-9_.:-]+)\]")


class CDPProvider(MessageProvider, ControlProvider):
    """Provider that sends/receives messages via CDP to an Electron IDE."""

    def __init__(self, agent_id, binding):
        self.agent_id = agent_id
        self.binding = dict(binding)
        self._backend = None
        self._pending_request_id = None

    def _ensure_backend(self):
        """Lazy-init the CDP backend."""
        if self._backend is not None:
            return self._backend

        from cdp.backends import get_backend
        from cdp.backends.base import CDPConfig

        backend_name = self.binding.get("backend", "antigravity")
        port = int(self.binding.get("cdp_port", 9333 if backend_name == "grok_bot" else 9000))
        config = CDPConfig(port=port)
        BackendClass = get_backend(backend_name)
        if backend_name == "grok_bot":
            self._backend = BackendClass(config, bot_name=self.binding["bot_name"])
        else:
            self._backend = BackendClass(config)
        return self._backend

    def deliver(self, envelope):
        if self.binding.get("backend") == "grok_bot":
            from providers.grok_worker import start_request
            return start_request(self.agent_id, self.binding, envelope)
        prompt = build_relay_prompt(envelope)
        delivered_at = time.time()

        try:
            backend = self._ensure_backend()
            result = backend.send_message(prompt)
        except Exception as exc:
            return DeliveryReceipt(
                request_id=envelope.request_id,
                status="failed",
                provider="cdp",
                delivered_at=delivered_at,
                error=str(exc),
            )

        if result == "SENT":
            if getattr(envelope, "expect_reply", True):
                self._pending_request_id = envelope.request_id
            return DeliveryReceipt(
                request_id=envelope.request_id,
                status="delivered",
                provider="cdp",
                delivered_at=delivered_at,
            )

        return DeliveryReceipt(
            request_id=envelope.request_id,
            status="failed",
            provider="cdp",
            delivered_at=delivered_at,
            error=result,
        )

    def poll_events(self, cursor=None):
        if not self._pending_request_id:
            return []

        try:
            backend = self._ensure_backend()
        except Exception:
            return []

        clicked, text = backend.poll_once(text_only=True)

        # Auto-click HITL buttons when idle
        if backend.state.consecutive_idle >= 3 and not clicked:
            try:
                backend.click_hitl_buttons()
            except Exception:
                pass

        if not text:
            return []

        # Search for reply marker anywhere in the transcript
        reply = extract_reply_from_transcript(text, self._pending_request_id)
        if reply is not None and reply.strip():
            # Emit event with content starting at the marker so
            # core/match.py:extract_relay_reply() can match it.
            request_id = self._pending_request_id
            marker_line = "[cc-relay reply_to=%s]" % self._pending_request_id
            event_content = marker_line + "\n" + reply
            self._pending_request_id = None
            return [
                RelayEvent(
                    event_type="message.reply",
                    request_id=request_id,
                    agent_id=self.agent_id,
                    content=event_content,
                    timestamp=time.time(),
                )
            ]

        return []

    def get_health(self):
        if self.binding.get("backend") == "grok_bot":
            return self._grok_health()
        port = int(self.binding.get("cdp_port", 9000))
        details = "backend=%s, port=%d" % (
            self.binding.get("backend", "antigravity"),
            port,
        )

        # Check CDP endpoint
        try:
            url = "http://127.0.0.1:%d/json/version" % port
            req = urllib.request.urlopen(url, timeout=2)
            data = json.loads(req.read())
            provider_status = "up"
            details += ", browser=%s" % data.get("Browser", "unknown")
        except Exception:
            provider_status = "down"

        # Check if backend can find a target
        agent_status = "unreachable"
        if provider_status == "up":
            try:
                backend = self._ensure_backend()
                targets = backend.list_targets()
                target = backend.find_target(targets)
                agent_status = "idle" if target else "target_missing"
            except Exception:
                agent_status = "target_error"

        return ProviderHealth(
            provider_status=provider_status,
            agent_status=agent_status,
            last_seen_at=None,
            last_delivery_at=None,
            details=details,
        )

    def _grok_health(self):
        import subprocess
        backend = self._ensure_backend()
        reachable, _ = backend.is_running()
        running = reachable
        if not running:
            try:
                running = subprocess.run(["pgrep", "-f", r"^/Applications/Grok Bot.app/Contents/MacOS/"],
                                         capture_output=True, timeout=3).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                pass
        details = "backend=grok_bot, port=%d, app_running=%s, target_bot=%s" % (
            backend.config.port, str(running).lower(), backend.bot_name)
        status = "unreachable"
        if reachable:
            try:
                snapshot = backend.health_snapshot()
                status = "busy" if snapshot["busy"] else "idle"
                details += ", current_bot=%s, busy=%s (current Bot)" % (snapshot["name"], str(snapshot["busy"]).lower())
            except Exception as exc:
                status = "target_error"
                details += ", error=%s" % exc
        return ProviderHealth("up" if reachable else "down", status, None, None, details)

    def supports_control(self):
        # A leading slash is ordinary Grok task text, never an IDE control command.
        return self.binding.get("backend") != "grok_bot"
