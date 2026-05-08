"""Auto-probe and self-healing mechanism for IDE UI changes.

When selectors break (e.g., Antigravity updates its UI), this module:
1. Detects the failure pattern (NO_INPUT, NOT_FOUND, etc.)
2. Dumps DOM snapshot + screenshot for diagnosis
3. Generates candidate selectors via heuristics
4. Self-tests candidates against live DOM
5. Persists the working fix back to the skill scripts

This is the key differentiator over CLI-based approaches — the LLM in the
loop can reason about unknown UI states and adapt without code redeployment.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from cdp.backends.base import CDPConnection

if TYPE_CHECKING:
    from cdp.backends.base import ElectronCDPBackend


@dataclass
class ProbeResult:
    """Result of a UI probe attempt."""
    target: str          # what we were looking for (e.g., "chat_input", "accept_all")
    found: bool          # whether we found it
    method: str          # selector method that worked (or "NONE")
    selector: str        # the working JS expression
    candidates_tried: int
    screenshot_path: str = ""
    dom_snapshot: str = ""
    error: str = ""


@dataclass
class HealReport:
    """Report from a self-healing attempt."""
    target: str
    success: bool
    old_selector: str
    new_selector: str
    probe_result: Optional[ProbeResult] = None
    persisted: bool = False
    error: str = ""


# ── Selector Strategies ──
# Each strategy is a JS expression generator that takes no args and returns
# an element or null. We try them in order until one matches.

CHAT_INPUT_STRATEGIES = [
    # Strategy 1: role="textbox" with position filter (current default)
    {
        "name": "role_textbox_position",
        "description": "contenteditable textbox in bottom-right area",
        "js": """
        (function() {
            var inputs = document.querySelectorAll('[role="textbox"]');
            for (var inp of inputs) {
                var rect = inp.getBoundingClientRect();
                if (rect.y > 600 && rect.width > 200) {
                    return 'FOUND';
                }
            }
            return 'NOT_FOUND';
        })()
        """,
        "focus_js": """
        (function() {
            var inputs = document.querySelectorAll('[role="textbox"]');
            for (var inp of inputs) {
                var rect = inp.getBoundingClientRect();
                if (rect.y > 600 && rect.width > 200) {
                    inp.focus();
                    inp.textContent = '';
                    inp.dispatchEvent(new Event('input', {bubbles: true}));
                    return "FOCUSED";
                }
            }
            return "NO_INPUT";
        })()
        """,
        "verify_js": """
        (function() {
            var inputs = document.querySelectorAll('[role="textbox"]');
            for (var inp of inputs) {
                var rect = inp.getBoundingClientRect();
                if (rect.y > 600 && rect.width > 200) {
                    return inp.textContent.length === 0 ? 'SENT' : 'NOT_SENT';
                }
            }
            return 'NOT_FOUND';
        })()
        """,
    },
    # Strategy 2: Any role="textbox" (broader match)
    {
        "name": "any_role_textbox",
        "description": "any contenteditable textbox",
        "js": """
        (function() {
            var el = document.querySelector('[role="textbox"]');
            return el ? 'FOUND' : 'NOT_FOUND';
        })()
        """,
        "focus_js": """
        (function() {
            var el = document.querySelector('[role="textbox"]');
            if (!el) return "NO_INPUT";
            el.focus();
            el.textContent = '';
            el.dispatchEvent(new Event('input', {bubbles: true}));
            return "FOCUSED";
        })()
        """,
        "verify_js": """
        (function() {
            var el = document.querySelector('[role="textbox"]');
            if (!el) return 'NOT_FOUND';
            return el.textContent.length === 0 ? 'SENT' : 'NOT_SENT';
        })()
        """,
    },
    # Strategy 3: textarea element
    {
        "name": "textarea",
        "description": "standard textarea element",
        "js": """
        (function() {
            var ta = document.querySelector('textarea');
            if (ta && ta.getBoundingClientRect().y > 300) return 'FOUND';
            return 'NOT_FOUND';
        })()
        """,
        "focus_js": """
        (function() {
            var ta = document.querySelector('textarea');
            if (!ta || ta.getBoundingClientRect().y <= 300) return "NO_INPUT";
            ta.focus();
            ta.value = '';
            ta.dispatchEvent(new Event('input', {bubbles: true}));
            return "FOCUSED";
        })()
        """,
        "verify_js": """
        (function() {
            var ta = document.querySelector('textarea');
            if (!ta) return 'NOT_FOUND';
            return ta.value.length === 0 ? 'SENT' : 'NOT_SENT';
        })()
        """,
    },
    # Strategy 4: contenteditable div (not role-based)
    {
        "name": "contenteditable",
        "description": "any contenteditable=true element",
        "js": """
        (function() {
            var els = document.querySelectorAll('[contenteditable="true"]');
            for (var el of els) {
                var rect = el.getBoundingClientRect();
                if (rect.y > 300 && rect.width > 200) return 'FOUND';
            }
            return 'NOT_FOUND';
        })()
        """,
        "focus_js": """
        (function() {
            var els = document.querySelectorAll('[contenteditable="true"]');
            for (var el of els) {
                var rect = el.getBoundingClientRect();
                if (rect.y > 300 && rect.width > 200) {
                    el.focus();
                    el.textContent = '';
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    return "FOCUSED";
                }
            }
            return "NO_INPUT";
        })()
        """,
        "verify_js": """
        (function() {
            var els = document.querySelectorAll('[contenteditable="true"]');
            for (var el of els) {
                var rect = el.getBoundingClientRect();
                if (rect.y > 300 && rect.width > 200) {
                    return el.textContent.length === 0 ? 'SENT' : 'NOT_SENT';
                }
            }
            return 'NOT_FOUND';
        })()
        """,
    },
    # Strategy 5: heuristic — find the largest editable element at the bottom
    {
        "name": "largest_bottom_editable",
        "description": "largest editable element in bottom half of screen",
        "js": """
        (function() {
            var candidates = [];
            var all = document.querySelectorAll('[contenteditable], [role="textbox"], textarea, input[type="text"]');
            for (var el of all) {
                var rect = el.getBoundingClientRect();
                if (rect.y > window.innerHeight * 0.5 && rect.width > 100) {
                    candidates.push({el: el, area: rect.width * rect.height, y: rect.y});
                }
            }
            if (candidates.length === 0) return 'NOT_FOUND';
            candidates.sort(function(a, b) { return b.area - a.area; });
            return 'FOUND';
        })()
        """,
        "focus_js": """
        (function() {
            var candidates = [];
            var all = document.querySelectorAll('[contenteditable], [role="textbox"], textarea, input[type="text"]');
            for (var el of all) {
                var rect = el.getBoundingClientRect();
                if (rect.y > window.innerHeight * 0.5 && rect.width > 100) {
                    candidates.push({el: el, area: rect.width * rect.height});
                }
            }
            if (candidates.length === 0) return "NO_INPUT";
            candidates.sort(function(a, b) { return b.area - a.area; });
            var el = candidates[0].el;
            el.focus();
            if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
                el.value = '';
            } else {
                el.textContent = '';
            }
            el.dispatchEvent(new Event('input', {bubbles: true}));
            return "FOCUSED";
        })()
        """,
        "verify_js": """
        (function() {
            var candidates = [];
            var all = document.querySelectorAll('[contenteditable], [role="textbox"], textarea, input[type="text"]');
            for (var el of all) {
                var rect = el.getBoundingClientRect();
                if (rect.y > window.innerHeight * 0.5 && rect.width > 100) {
                    candidates.push({el: el, area: rect.width * rect.height});
                }
            }
            if (candidates.length === 0) return 'NOT_FOUND';
            candidates.sort(function(a, b) { return b.area - a.area; });
            var el = candidates[0].el;
            var val = (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') ? el.value : el.textContent;
            return val.length === 0 ? 'SENT' : 'NOT_SENT';
        })()
        """,
    },
]

HITL_STRATEGIES = [
    # Strategy 1: span text matching (current default)
    {
        "name": "span_text_match",
        "description": "find span with exact text, click parent",
        "js_template": """
        (function() {{
            var spans = document.querySelectorAll('span');
            for (var s of spans) {{
                if (s.textContent.trim() === '{label}') {{
                    var t = s.closest('button') || s.closest('[role="button"]') || s.parentElement;
                    if (t) {{ t.click(); return 'CLICKED'; }}
                    s.click();
                    return 'CLICKED_SPAN';
                }}
            }}
            return 'NOT_FOUND';
        }})()
        """,
    },
    # Strategy 2: button text matching
    {
        "name": "button_text_match",
        "description": "find button with exact text",
        "js_template": """
        (function() {{
            var buttons = document.querySelectorAll('button');
            for (var b of buttons) {{
                if (b.textContent.trim() === '{label}') {{
                    b.click();
                    return 'CLICKED';
                }}
            }}
            return 'NOT_FOUND';
        }})()
        """,
    },
    # Strategy 3: aria-label matching
    {
        "name": "aria_label_match",
        "description": "find element by aria-label",
        "js_template": """
        (function() {{
            var el = document.querySelector('[aria-label="{label}"]');
            if (el) {{ el.click(); return 'CLICKED'; }}
            return 'NOT_FOUND';
        }})()
        """,
    },
    # Strategy 4: partial text match (fuzzy)
    {
        "name": "partial_text_match",
        "description": "any element containing the text",
        "js_template": """
        (function() {{
            var all = document.querySelectorAll('span, button, [role="button"]');
            for (var el of all) {{
                if (el.textContent.trim().includes('{label}') && el.getBoundingClientRect().height > 0) {{
                    el.click();
                    return 'CLICKED';
                }}
            }}
            return 'NOT_FOUND';
        }})()
        """,
    },
    # Strategy 5: data-testid matching
    {
        "name": "data_testid_match",
        "description": "find by data-testid attribute",
        "js_template": """
        (function() {{
            var testId = '{label}'.toLowerCase().replace(/\\s+/g, '-');
            var el = document.querySelector('[data-testid="' + testId + '"]')
                  || document.querySelector('[data-testid*="' + testId + '"]');
            if (el) {{ el.click(); return 'CLICKED'; }}
            return 'NOT_FOUND';
        }})()
        """,
    },
]


class UIProbe:
    """Probes the IDE's live DOM to find working selectors."""

    def __init__(self, conn):
        self.conn = conn

    def test_selector(self, js_expression):
        """Test a JS selector against live DOM. Returns (found, value)."""
        try:
            result = self.conn.evaluate(js_expression)
            found = result is not None and result != "NOT_FOUND" and result != "NO_INPUT"
            return found, result
        except Exception as e:
            return False, str(e)

    def find_working_strategy(self, strategies, target_name):
        """Try each strategy until one works. Returns the working strategy dict."""
        for i, strategy in enumerate(strategies):
            found, result = self.test_selector(strategy["js"])
            if found:
                return strategy
        return None

    def dump_dom_snapshot(self, max_chars=15000):
        """Dump a DOM snapshot for LLM analysis."""
        js = """
        (function() {
            function describe(el, depth) {
                if (depth > 4) return '';
                var indent = '  '.repeat(depth);
                var tag = el.tagName.toLowerCase();
                var attrs = '';
                if (el.id) attrs += ' id="' + el.id + '"';
                if (el.className && typeof el.className === 'string')
                    attrs += ' class="' + el.className.substring(0, 80) + '"';
                if (el.getAttribute('role')) attrs += ' role="' + el.getAttribute('role') + '"';
                if (el.getAttribute('contenteditable')) attrs += ' contenteditable';
                if (el.getAttribute('aria-label')) attrs += ' aria-label="' + el.getAttribute('aria-label') + '"';
                var rect = el.getBoundingClientRect();
                var pos = ' [' + Math.round(rect.x) + ',' + Math.round(rect.y) + ' ' + Math.round(rect.width) + 'x' + Math.round(rect.height) + ']';
                var line = indent + '<' + tag + attrs + '>' + pos;
                if (rect.width === 0 && rect.height === 0) return '';
                var children = '';
                for (var c of el.children) {
                    children += describe(c, depth + 1);
                }
                return line + '\\n' + children;
            }
            return describe(document.body, 0);
        })()
        """
        result = self.conn.evaluate(js)
        text = str(result or "")
        return text[:max_chars]

    def analyze_and_suggest(self, target, dom_snapshot):
        """Generate candidate selectors based on DOM analysis."""
        suggestions = []

        if target == "chat_input":
            if 'role="textbox"' in dom_snapshot:
                suggestions.append('[role="textbox"]')
            if 'contenteditable' in dom_snapshot:
                suggestions.append('[contenteditable="true"]')
            if '<textarea' in dom_snapshot:
                suggestions.append('textarea')
            for label in ['chat', 'message', 'input', 'prompt', 'type']:
                if label in dom_snapshot.lower():
                    suggestions.append('[aria-label*="%s" i]' % label)

        elif target.startswith("hitl:"):
            label = target.split(":", 1)[1]
            if label.lower() in dom_snapshot.lower():
                suggestions.append('span containing "%s"' % label)
                suggestions.append('button containing "%s"' % label)
                suggestions.append('[aria-label*="%s" i]' % label)

        return suggestions


class SelfHealer:
    """Self-healing mechanism: detect failures, probe for fixes, persist updates."""

    def __init__(self, backend, overrides_dir=""):
        self.backend = backend
        self.overrides_dir = overrides_dir or os.path.expanduser(
            "~/.hermes/ide-automation/overrides"
        )
        self.heal_history = []

    def heal_chat_input(self):
        """Try to find a working chat input selector."""
        conn = self.backend.ensure_conn()
        probe = UIProbe(conn)

        working = probe.find_working_strategy(CHAT_INPUT_STRATEGIES, "chat_input")
        if working:
            return HealReport(
                target="chat_input",
                success=True,
                old_selector="(broken)",
                new_selector=working["name"],
                probe_result=ProbeResult(
                    target="chat_input",
                    found=True,
                    method=working["name"],
                    selector=working["js"],
                    candidates_tried=len(CHAT_INPUT_STRATEGIES),
                ),
            )

        dom = probe.dump_dom_snapshot()
        screenshot_path = ""
        try:
            screenshot_path = self.backend.take_screenshot("/tmp/heal_chat_input.png")
        except Exception:
            pass

        suggestions = probe.analyze_and_suggest("chat_input", dom)

        report = HealReport(
            target="chat_input",
            success=False,
            old_selector="(all strategies failed)",
            new_selector="",
            probe_result=ProbeResult(
                target="chat_input",
                found=False,
                method="NONE",
                selector="",
                candidates_tried=len(CHAT_INPUT_STRATEGIES),
                screenshot_path=screenshot_path,
                dom_snapshot=dom,
            ),
            error="No strategy matched. Suggestions: %s" % suggestions,
        )
        self.heal_history.append(report)
        return report

    def heal_hitl_button(self, label):
        """Try to find a working HITL button click method."""
        conn = self.backend.ensure_conn()
        probe = UIProbe(conn)

        for strategy in HITL_STRATEGIES:
            js = strategy["js_template"].format(label=label)
            found, result = probe.test_selector(js)
            if found:
                return HealReport(
                    target="hitl:%s" % label,
                    success=True,
                    old_selector="(broken)",
                    new_selector=strategy["name"],
                    probe_result=ProbeResult(
                        target="hitl:%s" % label,
                        found=True,
                        method=strategy["name"],
                        selector=js,
                        candidates_tried=HITL_STRATEGIES.index(strategy) + 1,
                    ),
                )

        dom = probe.dump_dom_snapshot()
        report = HealReport(
            target="hitl:%s" % label,
            success=False,
            old_selector="(all strategies failed)",
            new_selector="",
            probe_result=ProbeResult(
                target="hitl:%s" % label,
                found=False,
                method="NONE",
                selector="",
                candidates_tried=len(HITL_STRATEGIES),
                dom_snapshot=dom,
            ),
            error="Button '%s' not found by any strategy" % label,
        )
        self.heal_history.append(report)
        return report

    def get_dom_for_llm_analysis(self, target="unknown"):
        """Export DOM snapshot + screenshot for LLM agent to analyze."""
        conn = self.backend.ensure_conn()
        probe = UIProbe(conn)

        dom = probe.dump_dom_snapshot()
        screenshot_path = ""
        try:
            screenshot_path = self.backend.take_screenshot("/tmp/ide_probe.png")
        except Exception:
            pass

        return {
            "target": target,
            "dom_snapshot": dom,
            "screenshot_path": screenshot_path,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "backend": self.backend.name,
            "suggestion": (
                "Analyze the DOM snapshot and screenshot to identify the correct "
                "selector for '%s'. Then test it with the UIProbe.test_selector() "
                "method. If it works, persist the fix." % target
            ),
        }

    def persist_fix(self, target, strategy_name, new_js,
                    focus_js="", verify_js=""):
        """Persist a working selector fix. Returns path to the overrides file."""
        os.makedirs(self.overrides_dir, exist_ok=True)
        overrides_file = os.path.join(self.overrides_dir, "%s.json" % self.backend.name)

        existing = {}
        if os.path.exists(overrides_file):
            with open(overrides_file) as f:
                existing = json.load(f)

        override = {
            "strategy_name": strategy_name,
            "test_js": new_js,
            "persisted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "backend": self.backend.name,
        }
        if focus_js:
            override["focus_js"] = focus_js
        if verify_js:
            override["verify_js"] = verify_js

        existing[target] = override

        with open(overrides_file, "w") as f:
            json.dump(existing, f, indent=2)

        return overrides_file

    def load_overrides(self):
        """Load persisted selector overrides."""
        overrides_file = os.path.join(
            self.overrides_dir, "%s.json" % self.backend.name
        )
        if os.path.exists(overrides_file):
            with open(overrides_file) as f:
                return json.load(f)
        return {}

    def auto_heal_on_failure(self, operation, error_value):
        """Trigger auto-healing based on error patterns."""
        if error_value in ("NO_INPUT", "NOT_FOUND", "FOCUSED_FALLBACK"):
            if operation == "chat_input":
                return self.heal_chat_input()
        elif "CLICKED" not in str(error_value) and operation.startswith("hitl:"):
            label = operation.split(":", 1)[1]
            return self.heal_hitl_button(label)
        return None
