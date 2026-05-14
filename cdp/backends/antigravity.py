"""Google Antigravity IDE backend — CDP-based automation.

Antigravity-specific quirks:
- Electron app, needs `--remote-debugging-port` and `--remote-allow-origins=*`
- Chat input is a contenteditable div with role="textbox" (not <textarea>)
- Multiple textboxes exist — chat input is bottom-right (y>600, width>200)
- Buttons may be in Shadow DOM — span-based clicking as primary strategy
- Scratchpad content is NOT in DOM — must ask IDE to write to disk
- HITL buttons: Accept all, Run, Proceed (auto-accept plugin misses file diffs)
"""

from __future__ import annotations

import time

from cdp.backends.base import ElectronCDPBackend, CDPConfig, CDPConnection


class AntigravityBackend(ElectronCDPBackend):
    name = "antigravity"
    default_port = 9000

    hitl_button_labels = [
        "Accept all", "Proceed", "Run", "Continue", "Allow",
    ]
    click_last_labels = {"Run", "Continue"}

    def find_target(self, targets):
        """Find Antigravity's workbench page.

        Priority:
        1. Page with 'workbench' in URL
        2. First page-type target (fallback)
        """
        pages = [t for t in targets if t["type"] == "page"]
        # Prefer workbench
        wb = [p for p in pages if "workbench" in p.get("url", "")]
        return (wb or pages)[0] if pages else None

    def get_chat_focus_js(self):
        """Focus the bottom-right chat input (not search bar)."""
        return """
        (function() {
            var panel = document.querySelector('.antigravity-agent-side-panel');
            var scoped = panel ? panel.querySelectorAll('[role="textbox"]') : [];
            for (var inp of scoped) {
                var rect = inp.getBoundingClientRect();
                var label = inp.getAttribute('aria-label') || inp.getAttribute('placeholder') || '';
                if (rect.width > 150 && rect.height > 20 && (label.includes('Message') || rect.x > 900)) {
                    inp.focus();
                    inp.textContent = '';
                    inp.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'deleteContentBackward'}));
                    return "FOCUSED";
                }
            }
            var inputs = document.querySelectorAll('[role="textbox"]');
            for (var inp of inputs) {
                var rect = inp.getBoundingClientRect();
                if (rect.x > 900 && rect.width > 200) {
                    inp.focus();
                    inp.textContent = '';
                    inp.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'deleteContentBackward'}));
                    return "FOCUSED";
                }
            }
            return "NO_INPUT";
        })()
        """

    def get_chat_verify_js(self):
        """Check if the chat input is empty (= message was sent)."""
        return """
        (function() {
            var panel = document.querySelector('.antigravity-agent-side-panel');
            var scoped = panel ? panel.querySelectorAll('[role="textbox"]') : [];
            for (var inp of scoped) {
                var rect = inp.getBoundingClientRect();
                var label = inp.getAttribute('aria-label') || inp.getAttribute('placeholder') || '';
                if (rect.width > 150 && rect.height > 20 && (label.includes('Message') || rect.x > 900)) {
                    return inp.textContent.length === 0 ? 'SENT' : 'NOT_SENT';
                }
            }
            var inputs = document.querySelectorAll('[role="textbox"]');
            for (var inp of inputs) {
                var rect = inp.getBoundingClientRect();
                if (rect.x > 900 && rect.width > 200) {
                    return inp.textContent.length === 0 ? 'SENT' : 'NOT_SENT';
                }
            }
            return 'NOT_SENT';
        })()
        """

    def switch_model(self, model_name):
        """Switch the active model in the chat panel.

        Opens the model dropdown, finds the target model, and clicks it.
        Returns 'SWITCHED', 'NOT_FOUND', or 'DROPDOWN_FAILED'.
        """
        conn = self.ensure_conn()

        # Step 1: Click current model name to open dropdown
        open_js = """
        (function() {
            var spans = document.querySelectorAll('span');
            for (var s of spans) {
                var text = s.textContent.trim();
                if ((text.includes('Claude') || text.includes('Gemini') || text.includes('GPT'))
                    && text.length < 60 && s.getBoundingClientRect().y > 700) {
                    var rect = s.getBoundingClientRect();
                    s.click();
                    return JSON.stringify({x: rect.x + rect.width/2, y: rect.y + rect.height/2, text: text});
                }
            }
            return 'NOT_FOUND';
        })()
        """
        result = conn.evaluate(open_js)
        if not result or result == "NOT_FOUND":
            return "DROPDOWN_FAILED"

        time.sleep(0.5)

        # Step 2: Find and click the target model
        click_js = """
        (function() {
            var all = document.querySelectorAll('*');
            for (var el of all) {
                var text = el.textContent.trim();
                if (text === '%s' && el.children.length === 0) {
                    el.click();
                    return 'CLICKED';
                }
            }
            return 'NOT_FOUND';
        })()
        """ % model_name
        result = conn.evaluate(click_js)
        if result == "CLICKED":
            time.sleep(0.5)
            return "SWITCHED"
        return "NOT_FOUND"

    def list_models(self):
        """Try to list available models by opening the dropdown."""
        conn = self.ensure_conn()

        # Open dropdown
        conn.evaluate("""
        (function() {
            var spans = document.querySelectorAll('span');
            for (var s of spans) {
                var text = s.textContent.trim();
                if ((text.includes('Claude') || text.includes('Gemini') || text.includes('GPT'))
                    && text.length < 60 && s.getBoundingClientRect().y > 700) {
                    s.click();
                    return 'OPENED';
                }
            }
            return 'NOT_FOUND';
        })()
        """)

        time.sleep(0.5)

        # Extract model names
        models = conn.evaluate("""
        (function() {
            var models = [];
            var seen = {};
            var all = document.querySelectorAll('*');
            for (var el of all) {
                var text = el.textContent.trim();
                if ((text.includes('Claude') || text.includes('Gemini') || text.includes('GPT'))
                    && text.length < 60 && text.length > 5
                    && el.children.length === 0 && !seen[text]) {
                    seen[text] = true;
                    models.push(text);
                }
            }
            return JSON.stringify(models);
        })()
        """)

        # Close dropdown by pressing Escape
        for key_type in ("keyDown", "keyUp"):
            conn.send("Input.dispatchKeyEvent", {
                "type": key_type,
                "key": "Escape",
                "code": "Escape",
                "windowsVirtualKeyCode": 27,
                "nativeVirtualKeyCode": 27,
            })

        if isinstance(models, str):
            import json
            try:
                return json.loads(models)
            except Exception:
                pass
        return []
