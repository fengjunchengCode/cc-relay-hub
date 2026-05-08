"""Generic CDP-based backend for Electron IDEs.

Many Electron-based IDEs (Antigravity, Cursor, VS Code) share the same
underlying CDP protocol. This base class implements the common mechanics:

- CDP connection management (connect, reconnect, target discovery)
- Chat input: focus + Input.insertText + Enter dispatch
- HITL button clicking: span/button scanning + coordinate fallback
- Screenshot capture
- Text extraction via innerText

Subclasses override `find_target()`, `find_chat_input()`, and
`hitl_button_labels` to handle IDE-specific quirks.
"""

from __future__ import annotations

import abc
import base64
import json
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class CDPConfig:
    """CDP connection settings."""
    port: int = 9000
    origins: str = "*"
    connect_timeout: int = 10
    recv_timeout: int = 30


@dataclass
class PollState:
    """Tracks state across polling cycles."""
    last_text: str = ""
    consecutive_idle: int = 0
    total_polls: int = 0
    total_clicks: int = 0
    last_click_labels: list = field(default_factory=list)


class CDPConnection:
    """Manages a WebSocket connection to a CDP target."""

    def __init__(self, ws_url, timeout=10):
        import websocket
        self._ws_url = ws_url
        self._timeout = timeout
        self._ws = websocket.create_connection(ws_url, timeout=timeout)
        self._msg_id = 0

    @property
    def ws(self):
        return self._ws

    def next_id(self):
        self._msg_id += 1
        return self._msg_id

    def send(self, method, params=None):
        """Send a CDP command and return the result."""
        msg = {"id": self.next_id(), "method": method}
        if params:
            msg["params"] = params
        self._ws.send(json.dumps(msg))
        resp = json.loads(self._ws.recv())
        return resp.get("result", resp)

    def evaluate(self, expression):
        """Evaluate JS expression and return the value."""
        resp = self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True
        })
        return resp.get("result", {}).get("value", None)

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass

    def reconnect(self, ws_url):
        """Reconnect to a (possibly new) target URL."""
        self.close()
        import websocket
        self._ws = websocket.create_connection(ws_url, timeout=self._timeout)
        self._ws_url = ws_url


class ElectronCDPBackend(abc.ABC):
    """Abstract base for CDP-controlled Electron IDEs."""

    # ── Subclass must define ──
    name = "generic"
    default_port = 9000

    # HITL buttons to auto-click, in priority order
    hitl_button_labels = [
        "Accept all", "Proceed", "Run", "Continue", "Allow",
    ]
    # Labels where we click the LAST occurrence (not first)
    click_last_labels = {"Run", "Continue"}

    def __init__(self, config=None):
        self.config = config or CDPConfig(port=self.default_port)
        self.conn = None
        self.state = PollState()
        self._healer = None
        self._auto_heal_enabled = False

    def enable_auto_heal(self, overrides_dir=""):
        """Enable the self-healing mechanism."""
        from cdp.auto_heal import SelfHealer
        self._healer = SelfHealer(self, overrides_dir)
        self._auto_heal_enabled = True

    def _try_heal(self, target, error_value):
        """Attempt self-healing. Returns new JS expression or None."""
        if not self._auto_heal_enabled or not self._healer:
            return None
        report = self._healer.auto_heal_on_failure(target, error_value)
        if report and report.success and report.probe_result:
            from cdp.auto_heal import CHAT_INPUT_STRATEGIES, HITL_STRATEGIES
            strategies = CHAT_INPUT_STRATEGIES if target == "chat_input" else HITL_STRATEGIES
            for s in strategies:
                if s["name"] == report.new_selector:
                    return s.get("focus_js", None)
        return None

    # ── CDP target resolution ──

    @property
    def cdp_base(self):
        return "http://localhost:%d" % self.config.port

    def is_running(self):
        """Check if CDP is accessible."""
        try:
            r = urllib.request.urlopen(
                "%s/json/version" % self.cdp_base, timeout=3
            )
            data = json.loads(r.read())
            return True, data.get("Browser", "unknown")
        except Exception as e:
            return False, str(e)

    def list_targets(self):
        """List all CDP targets."""
        return json.loads(
            urllib.request.urlopen("%s/json/list" % self.cdp_base).read()
        )

    @abc.abstractmethod
    def find_target(self, targets):
        """Find the main IDE page target. Subclasses override for IDE-specific matching."""
        ...

    def connect(self):
        """Connect to the IDE's main page target."""
        targets = self.list_targets()
        target = self.find_target(targets)
        if not target:
            raise RuntimeError(
                "No %s page target found. Is the IDE running with CDP?" % self.name
            )
        ws_url = target["webSocketDebuggerUrl"]
        self.conn = CDPConnection(ws_url, self.config.connect_timeout)
        return self.conn

    def ensure_conn(self):
        """Return existing connection or create new one."""
        if self.conn is None:
            return self.connect()
        return self.conn

    def reconnect(self):
        """Reconnect to current target (e.g. after page navigation)."""
        targets = self.list_targets()
        target = self.find_target(targets)
        if not target:
            raise RuntimeError("No %s target on reconnect" % self.name)
        if self.conn:
            self.conn.reconnect(target["webSocketDebuggerUrl"])
        else:
            self.connect()

    # ── Chat input ──

    @abc.abstractmethod
    def get_chat_focus_js(self):
        """Return JS expression that focuses the chat input and clears it.

        Must return 'FOCUSED', 'FOCUSED_FALLBACK', or 'NO_INPUT'.
        """
        ...

    @abc.abstractmethod
    def get_chat_verify_js(self):
        """Return JS expression that checks if chat input is empty.

        Must return 'SENT' or 'NOT_SENT'.
        """
        ...

    def send_message(self, text):
        """Type and send a message in the chat input.

        Returns 'SENT', 'NOT_SENT', or error string.
        """
        conn = self.ensure_conn()

        # Step 1: Focus
        focus_result = conn.evaluate(self.get_chat_focus_js())
        if focus_result and "NO_INPUT" in str(focus_result):
            # Attempt self-healing
            healed = self._try_heal("chat_input", "NO_INPUT")
            if healed:
                focus_result = conn.evaluate(healed)
                if focus_result and "NO_INPUT" in str(focus_result):
                    return "NO_INPUT"
            else:
                return "NO_INPUT"

        # Step 2: Insert text via CDP
        conn.send("Input.insertText", {"text": text})
        time.sleep(0.3)

        # Step 3: Press Enter (real keyboard event)
        for key_type in ("keyDown", "keyUp"):
            conn.send("Input.dispatchKeyEvent", {
                "type": key_type,
                "key": "Enter",
                "code": "Enter",
                "windowsVirtualKeyCode": 13,
                "nativeVirtualKeyCode": 13,
            })

        time.sleep(0.5)

        # Step 4: Verify
        verify = conn.evaluate(self.get_chat_verify_js())
        return str(verify or "UNKNOWN")

    # ── Reading responses ──

    def get_latest_text(self, chars=5000):
        """Get the tail of page innerText (latest chat response)."""
        conn = self.ensure_conn()
        text = conn.evaluate(
            "document.body.innerText.substring(document.body.innerText.length - %d)" % chars
        )
        return str(text or "")

    # ── HITL button clicking ──

    def _click_span_js(self, label, click_last=False):
        """Generate JS to click a span with given text."""
        if click_last:
            return """
            (function() {
                var spans = document.querySelectorAll('span');
                var matches = [];
                for (var s of spans) { if (s.textContent.trim() === '%s') matches.push(s); }
                if (matches.length > 0) {
                    var last = matches[matches.length - 1];
                    var t = last.closest('button') || last.closest('[role="button"]') || last.parentElement;
                    if (t) { t.click(); return 'CLICKED'; }
                    last.click();
                    return 'CLICKED_SPAN';
                }
                return 'NOT_FOUND';
            })()
            """ % label
        return """
        (function() {
            var spans = document.querySelectorAll('span');
            for (var s of spans) {
                if (s.textContent.trim() === '%s') {
                    var t = s.closest('button') || s.closest('[role="button"]') || s.parentElement;
                    if (t) { t.click(); return 'CLICKED'; }
                    s.click();
                    return 'CLICKED_SPAN';
                }
            }
            return 'NOT_FOUND';
        })()
        """ % label

    def click_hitl_buttons(self):
        """Click any pending HITL buttons. Returns list of clicked labels."""
        conn = self.ensure_conn()
        clicked = []
        for label in self.hitl_button_labels:
            click_last = label in self.click_last_labels
            result = conn.evaluate(self._click_span_js(label, click_last))
            if result and ("CLICKED" in str(result)):
                clicked.append(label)
                time.sleep(0.3)
        self.state.total_clicks += len(clicked)
        self.state.last_click_labels = clicked
        return clicked

    # ── Screenshot ──

    def take_screenshot(self, path="/tmp/ide_screenshot.png"):
        """Capture a PNG screenshot and save to path."""
        conn = self.ensure_conn()
        resp = conn.send("Page.captureScreenshot", {"format": "png"})
        img_data = base64.b64decode(resp.get("data", ""))
        with open(path, "wb") as f:
            f.write(img_data)
        return path

    # ── Poll / Monitor ──

    def poll_once(self, text_only=False, chars=5000):
        """Single poll cycle: click HITL buttons + get latest text.

        Returns (clicked_labels, latest_text).
        """
        clicked = []
        if not text_only:
            clicked = self.click_hitl_buttons()

        text = self.get_latest_text(chars)
        self.state.total_polls += 1

        # Track idle state
        if text == self.state.last_text:
            self.state.consecutive_idle += 1
        else:
            self.state.consecutive_idle = 0
        self.state.last_text = text

        return clicked, text

    # ── Lifecycle ──

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
