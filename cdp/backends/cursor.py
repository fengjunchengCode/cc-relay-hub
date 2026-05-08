"""Cursor IDE backend — CDP-based automation (stub).

TODO: Needs real testing to fill in selectors. Contributions welcome.
"""

from __future__ import annotations

from cdp.backends.base import ElectronCDPBackend, CDPConfig


class CursorBackend(ElectronCDPBackend):
    name = "cursor"
    default_port = 9222

    hitl_button_labels = [
        "Accept", "Accept All", "Apply", "Run", "Continue", "Proceed",
    ]
    click_last_labels = {"Run", "Continue"}

    def find_target(self, targets):
        """Find Cursor's main editor page.

        Priority:
        1. Page with 'cursor' in URL
        2. Page with 'workbench' in URL
        3. Page with 'editor' in URL
        4. First page-type target (fallback)
        """
        pages = [t for t in targets if t["type"] == "page"]
        for pattern in ("cursor", "workbench", "editor"):
            match = [p for p in pages if pattern in p.get("url", "").lower()]
            if match:
                return match[0]
        return pages[0] if pages else None

    def get_chat_focus_js(self):
        """Focus the chat input — try textarea first, then textbox."""
        return """
        (function() {
            // Try textarea first (Cursor uses textarea for chat)
            var tas = document.querySelectorAll('textarea');
            for (var ta of tas) {
                var rect = ta.getBoundingClientRect();
                if (rect.y > 300 && rect.width > 200) {
                    ta.focus();
                    ta.value = '';
                    ta.dispatchEvent(new Event('input', {bubbles: true}));
                    return "FOCUSED";
                }
            }
            // Fallback: role=textbox
            var inputs = document.querySelectorAll('[role="textbox"]');
            for (var inp of inputs) {
                var rect = inp.getBoundingClientRect();
                if (rect.y > 300 && rect.width > 200) {
                    inp.focus();
                    inp.textContent = '';
                    inp.dispatchEvent(new Event('input', {bubbles: true}));
                    return "FOCUSED_FALLBACK";
                }
            }
            return "NO_INPUT";
        })()
        """

    def get_chat_verify_js(self):
        """Check if chat input is empty."""
        return """
        (function() {
            var tas = document.querySelectorAll('textarea');
            for (var ta of tas) {
                var rect = ta.getBoundingClientRect();
                if (rect.y > 300 && rect.width > 200) {
                    return ta.value.length === 0 ? 'SENT' : 'NOT_SENT';
                }
            }
            var inputs = document.querySelectorAll('[role="textbox"]');
            for (var inp of inputs) {
                var rect = inp.getBoundingClientRect();
                if (rect.y > 300 && rect.width > 200) {
                    return inp.textContent.length === 0 ? 'SENT' : 'NOT_SENT';
                }
            }
            return 'NOT_FOUND';
        })()
        """
