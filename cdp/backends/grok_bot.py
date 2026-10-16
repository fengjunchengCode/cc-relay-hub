"""Grok Bot: serialized UI operations, independent per-bot conversations.

Only rendered messages are read. No page-tail truncation, app internals, or
model-written reply markers are involved in completion detection.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request

from cdp.backends.base import CDPConfig, ElectronCDPBackend


SNAPSHOT_JS = r'''(() => ({
 name: document.querySelector('.sand-chat-header__name')?.innerText?.trim() || '',
 messages: [...document.querySelectorAll('.sand-message-block .sand-message[data-role="user"], .sand-message-block .sand-message[data-role="assistant"]')]
 .map(m => ({role:m.getAttribute('data-role'),
   row_index:Number(m.closest('.sand-virtual-transcript__row')?.getAttribute('data-index') ?? -1),
   part_index:[...(m.closest('.sand-virtual-transcript__row') || m.parentElement).querySelectorAll('.sand-message[data-role]')].indexOf(m),
   text:(m.querySelector('.sand-message-prose')?.innerText ?? m.innerText).trim(),
   images:[...m.querySelectorAll('img')].filter(i => !i.closest('.sand-avatar')).length})),
 busy: !!document.querySelector('.sand-chat-stage .sand-activity-mark, .sand-chat-stage [data-activity]')
   || [...document.querySelectorAll('.sand-chat button')].some(b => /停止|Stop/i.test(b.innerText + (b.getAttribute('aria-label') || '')))
   || /正在工作\s*$/.test((document.querySelector('.sand-chat')?.innerText || '').trim()),
 draft:(document.querySelector('.sand-prompt-field')?.innerText || '').trim()
}))()'''


def runtime_dir():
    root = Path(os.environ.get('CC_RELAY_HUB_DIR') or Path.home() / '.cc-connect' / 'cc-relay-hub')
    path = root / '.cc-relay-hub' / 'grok'
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


class GrokNotSentError(RuntimeError):
    """Definitive rejection before modifying the input or pressing Enter."""


class FileLease:
    """flock works across threads/processes. close (not LOCK_UN) permits fd handoff."""
    def __init__(self, path, timeout=10):
        import fcntl
        self.fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    self.fd = None
                    raise RuntimeError('Grok operation busy: %s' % Path(path).name)
                time.sleep(0.05)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def message_key(message):
    return (message.get('role'), message.get('text', ''), int(message.get('images', 0)))


def compatible(previous, current):
    if message_key(previous) == message_key(current):
        return True
    # A currently streaming assistant message may grow between snapshots.
    return (previous.get('role') == current.get('role') == 'assistant'
            and (previous.get('text', '') == current.get('text', '')
                 or (bool(previous.get('text')) and current.get('text', '').startswith(previous['text'])))
            and current.get('images', 0) >= previous.get('images', 0))


def added_since(before, after):
    """Return new suffix when old DOM messages survive or the beginning is evicted."""
    for size in range(min(len(before), len(after)), 0, -1):
        if all(message_key(a) == message_key(b) for a, b in zip(before[-size:], after[:size])):
            return after[size:]
    return []


def acknowledgement_only(replies):
    if not replies or len(replies) > 2:
        return False
    start = re.compile(r"^(好[的啊]?|收到|明白|没问题|开始|正在|稍等|我先|我会|我这就|浏览器已经|查完|got it|okay|ok\b|understood|i['’]?ll)", re.I)
    work = re.compile(r'查|看|处理|开始|稍等|回复|发你|给你|完成|收到|明白|research|check|work|send|report|got it|okay|ok\b|understood', re.I)
    return all(len(r.strip()) <= 240 and (re.fullmatch(r'好[的啊]?[。！!~～]*', r.strip())
               or (start.search(r.strip()) and work.search(r.strip()))) for r in replies)


class ReplyTracker:
    """Collect the full assistant sequence behind a unique user-message anchor."""
    def __init__(self, request_id, started_at, timeout=1800, quiet=60, chase_after=180):
        self.request_id = request_id
        self.anchor = '[cc-relay task_id=%s]' % request_id
        self.followup = '[cc-relay followup_id=%s]' % request_id
        self.deadline = started_at + timeout
        self.quiet = quiet
        self.chase_after = chase_after
        self.history = []
        self.last_change = started_at
        self.idle_since = None
        self.signature = ''
        self.chased = False
        self.conflict = False

    def observe(self, snapshot, now):
        messages = snapshot.get('messages', [])
        anchors = [i for i, m in enumerate(messages) if m.get('role') == 'user' and self.anchor in m.get('text', '')]
        attributed = bool(anchors)
        if anchors:
            self.history = messages[anchors[-1]:]
        elif self.history:
            # Virtualized DOM: retain earlier chunks and align the largest overlap.
            for size in range(min(len(self.history), len(messages)), 0, -1):
                if all(compatible(a, b) for a, b in zip(self.history[-size:], messages[:size])):
                    self.history = self.history[:-size] + messages
                    attributed = True
                    break
        # Another human/agent used this Bot: do not mix their answer into ours.
        foreign = next((i for i, m in enumerate(self.history[1:], 1)
                        if m.get('role') == 'user' and self.followup not in m.get('text', '')), None)
        if foreign is not None:
            self.history = self.history[:foreign]
            self.conflict = True
        signature = json.dumps(self.replies(), ensure_ascii=False)
        if signature != self.signature:
            self.signature = signature
            self.last_change = now
        if snapshot.get('busy') or not attributed:
            self.idle_since = None
        elif self.idle_since is None:
            self.idle_since = now
        if self.conflict or now >= self.deadline:
            return 'timeout'
        replies = self.replies()
        if self.idle_since is None or not replies:
            return 'wait'
        stable = now - max(self.last_change, self.idle_since)
        if acknowledgement_only(replies):
            if not self.chased and stable >= self.chase_after:
                self.chased = True
                return 'chase'
            return 'wait'
        return 'complete' if stable >= self.quiet else 'wait'

    def replies(self):
        result = []
        for message in self.history[1:]:
            if message.get('role') != 'assistant':
                continue
            text = message.get('text', '').strip()
            count = int(message.get('images', 0))
            if count:
                text += ('\n' if text else '') + '[图片 %d 张，未取回]' % count
            if text:
                result.append(text)
        return result

    def render(self, timeout=False, reason=''):
        body = '\n\n'.join(self.replies())
        if timeout:
            warning = reason or ('检测到另一条用户消息，已停止归属后续回复。' if self.conflict else '等待超时，以下是已收到的内容。')
            return '[TIMEOUT] %s\n\n%s' % (warning, body or '尚未收到可归属本次任务的回复。')
        return body

    def to_dict(self):
        return dict(self.__dict__)

    @classmethod
    def restore(cls, value):
        tracker = cls(value['request_id'], 0)
        tracker.__dict__.update(value)
        return tracker


class GrokConnection:
    """Match CDP response IDs; unsolicited events must never become command results."""
    def __init__(self, url):
        import websocket
        self.ws = websocket.create_connection(url, timeout=15, suppress_origin=True)
        self.sequence = 0

    def send(self, method, params=None):
        self.sequence += 1
        request_id = self.sequence
        self.ws.send(json.dumps({'id': request_id, 'method': method, 'params': params or {}}))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            message = json.loads(self.ws.recv())
            if message.get('id') != request_id:
                continue
            if 'error' in message:
                raise RuntimeError('CDP %s: %s' % (method, message['error'].get('message')))
            return message.get('result', {})
        raise TimeoutError('CDP response timeout: %s' % method)

    def evaluate(self, expression):
        result = self.send('Runtime.evaluate', {'expression': expression, 'returnByValue': True, 'awaitPromise': True})
        if result.get('exceptionDetails'):
            raise RuntimeError('Grok DOM evaluation failed: %s' % result['exceptionDetails'].get('text'))
        return result.get('result', {}).get('value')

    def close(self):
        self.ws.close()


class GrokBotBackend(ElectronCDPBackend):
    name = 'grok_bot'
    default_port = 9333
    hitl_button_labels = []

    def __init__(self, config=None, bot_name='总管'):
        super().__init__(config or CDPConfig(port=self.default_port))
        self.bot_name = bot_name

    @property
    def cdp_base(self):
        return 'http://127.0.0.1:%d' % self.config.port

    def app_lock(self):
        return FileLease(runtime_dir() / ('app-%d.lock' % self.config.port), timeout=20)

    def bot_lease(self):
        key = hashlib.sha256(('%s:%s' % (self.config.port, self.bot_name)).encode()).hexdigest()[:24]
        return FileLease(runtime_dir() / ('bot-%s.lock' % key), timeout=0)

    def list_targets(self):
        with urllib.request.urlopen(self.cdp_base + '/json/list', timeout=3) as response:
            return json.load(response)

    def find_target(self, targets):
        return next((t for t in targets if t.get('type') == 'page' and
                     ('grok bot' in t.get('title', '').lower() or 'grok%20bot.app' in t.get('url', '').lower())), None)

    def connect(self):
        target = self.find_target(self.list_targets())
        if not target:
            raise RuntimeError('Grok Bot page target missing')
        self.conn = GrokConnection(target['webSocketDebuggerUrl'])
        return self.conn

    def ensure_running(self):
        with self.app_lock():
            if self.is_running()[0]:
                return
            subprocess.run(['open', '-a', 'Grok Bot', '--args', '--remote-debugging-port=%d' % self.config.port], check=True, timeout=10, capture_output=True)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self.is_running()[0]:
                    return
                time.sleep(0.5)
            raise RuntimeError('Grok Bot did not expose CDP; if already open without debugging, restart it normally with port 9333. The relay will not kill the app.')

    def _select(self):
        conn = self.ensure_conn()
        name = conn.evaluate("document.querySelector('.sand-chat-header__name')?.innerText?.trim() || ''")
        if name != self.bot_name:
            expression = r'''(() => {
              const e=[...document.querySelectorAll('button,[role=button],a,li')].find(e=>{
                const r=e.getBoundingClientRect();return r.left<280 && r.width>150 && r.height>30 && r.height<90
                  && (e.innerText||'').split('\n')[0].trim()===BOT_NAME;
              });if(!e)return false;e.click();return true;
            })()'''.replace('BOT_NAME', json.dumps(self.bot_name, ensure_ascii=False))
            if not conn.evaluate(expression):
                raise RuntimeError('Grok Bot sidebar entry missing: %s' % self.bot_name)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if conn.evaluate("document.querySelector('.sand-chat-header__name')?.innerText?.trim() || ''") == self.bot_name:
                    # Header switches before rendered conversation finishes replacing.
                    time.sleep(0.4)
                    return
                time.sleep(0.1)
            raise RuntimeError('Grok Bot header did not match requested bot')

    def _snapshot(self):
        snapshot = self.ensure_conn().evaluate(SNAPSHOT_JS)
        if not isinstance(snapshot, dict) or snapshot.get('name') != self.bot_name:
            raise RuntimeError('Grok Bot changed during operation; refusing to attribute messages')
        return snapshot

    def _collect_to_anchor(self, anchor):
        """Walk rendered UI backwards: a fast split answer can evict every overlap.

        data-index is the observed virtual transcript row index; part_index
        distinguishes several assistant articles grouped into the same row.
        No app stores or hidden conversation APIs are accessed.
        """
        conn = self.ensure_conn()
        collected = {}
        deadline = time.monotonic() + 12

        def collect(value):
            for message in value['messages']:
                key = (message.get('row_index', -1), message.get('part_index', -1))
                if min(key) < 0:
                    raise RuntimeError('Grok virtual transcript row identity missing')
                collected[key] = message

        try:
            # A human or a previous reader may have left the view above its end.
            conn.evaluate("(() => {const e=document.querySelector('.sand-virtual-transcript');if(e)e.scrollTop=e.scrollHeight})()")
            time.sleep(0.15)
            latest = self._snapshot()
            collect(latest)
            while not any(m['role'] == 'user' and anchor in m['text'] for m in collected.values()):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Grok transcript scan incomplete; refusing a truncated successful reply')
                moved = conn.evaluate("""(() => {const e=document.querySelector('.sand-virtual-transcript');
                  if(!e)return false;const before=e.scrollTop;
                  e.scrollTop=Math.max(0,before-Math.max(200,e.clientHeight*0.8));
                  return e.scrollTop<before;})()""")
                if not moved:
                    raise RuntimeError('Grok task anchor is absent from rendered transcript')
                time.sleep(0.15)
                collect(self._snapshot())
            latest['messages'] = [collected[key] for key in sorted(collected)]
            return latest
        finally:
            conn.evaluate("(() => {const e=document.querySelector('.sand-virtual-transcript');if(e)e.scrollTop=e.scrollHeight})()")

    def read_snapshot(self, request_id=None):
        with self.app_lock():
            try:
                self._select()
                if request_id:
                    self.ensure_conn().evaluate("(() => {const e=document.querySelector('.sand-virtual-transcript');if(e)e.scrollTop=e.scrollHeight})()")
                    time.sleep(0.15)
                snapshot = self._snapshot()
                if request_id:
                    anchor = '[cc-relay task_id=%s]' % request_id
                    if not any(m['role'] == 'user' and anchor in m['text'] for m in snapshot['messages']):
                        snapshot = self._collect_to_anchor(anchor)
                return snapshot
            finally:
                self.close()

    def _send_text(self, text):
        snapshot = self._snapshot()
        if snapshot['busy']:
            raise GrokNotSentError('Grok Bot is busy with existing work; no message sent')
        if snapshot['draft']:
            raise GrokNotSentError('Grok Bot has an unsent draft; refusing to overwrite it')
        conn = self.ensure_conn()
        if conn.evaluate(self.get_chat_focus_js()) != 'FOCUSED':
            raise GrokNotSentError('Grok input missing')
        conn.send('Input.insertText', {'text': text})
        # Validate the destination again immediately before Enter.
        if conn.evaluate("document.querySelector('.sand-chat-header__name')?.innerText?.trim() || ''") != self.bot_name:
            raise RuntimeError('Grok Bot changed before Enter; message not submitted')
        time.sleep(0.3)
        for kind in ('keyDown', 'keyUp'):
            conn.send('Input.dispatchKeyEvent', {'type': kind, 'key': 'Enter', 'code': 'Enter', 'windowsVirtualKeyCode': 13})

    def begin(self, body, request_id):
        anchor = '[cc-relay task_id=%s]' % request_id
        question = body + '\n\n' + anchor + '\n（上面是任务标识，回答无需复述标识。）'
        entered_send = False
        try:
            self.ensure_running()
            with self.app_lock():
                try:
                    self._select()
                    entered_send = True
                    self._send_text(question)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        after = self._snapshot()
                        if any(m['role'] == 'user' and anchor in m['text'] for m in after['messages']):
                            return after
                        time.sleep(0.2)
                    raise RuntimeError('Send unconfirmed; inspect this Bot before retrying to avoid duplicates')
                finally:
                    self.close()
        except Exception as exc:
            if not entered_send:
                raise GrokNotSentError(str(exc)) from exc
            raise

    def chase(self, request_id):
        with self.app_lock():
            try:
                self._select()
                self._send_text('请把完整结果一次发出来\n[cc-relay followup_id=%s]' % request_id)
            finally:
                self.close()

    def get_chat_focus_js(self):
        return "(() => {const f=document.querySelector('.sand-prompt-field');if(!f)return 'NO_INPUT';f.focus();return 'FOCUSED'})()"

    def get_chat_verify_js(self):
        return "document.querySelector('.sand-prompt-field')?.innerText.trim() ? 'NOT_SENT' : 'SENT'"

    def health_snapshot(self):
        # Health is observational: do not switch bots or auto-start the application.
        with self.app_lock():
            try:
                return self.ensure_conn().evaluate(SNAPSHOT_JS)
            finally:
                self.close()
