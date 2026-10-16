"""Isolated executable acceptance: real CLI + fake cc-connect + real test hook.

No production registry, state DB, hook port, session, or other agent is used.
Run: python3 -m unittest tests.test_delivery_queue_e2e -v
"""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

import sys as _test_sys
from pathlib import Path as _TestPath
_test_sys.path.insert(0, str(_TestPath(__file__).resolve().parents[1]))
from tests import require_sandbox
require_sandbox()

from core.state import StateStore
from core.delivery_queue import DeliveryQueue

ROOT = Path(__file__).resolve().parents[1]


def wait_until(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate(): return True
        threading.Event().wait(.05)
    return bool(predicate())


@unittest.skipUnless(shutil.which('node'), 'Node required for fake hook acceptance')
class IsolatedQueueAcceptance(unittest.TestCase):
    def test_busy_queue_idle_deliver_reply_to_origin_restart_and_notice_fifo(self):
        with tempfile.TemporaryDirectory(prefix='t10-e2e-') as folder:
            root = Path(folder)
            sessions = root/'sessions'; sessions.mkdir()
            history_file = sessions/'codex-bot_test.json'
            db = root/'state.db'; store = StateStore(str(db)); queue = DeliveryQueue(store)
            lock = threading.Lock()
            data = {'active_session': {'test:target':'target','test:origin':'origin'},
                    'user_sessions': {'test:target':['target'],'test:origin':['origin']},
                    'sessions': {'target':{'history':[]}, 'origin':{'history':[]}}}
            def append(session, role, text):
                with lock:
                    data['sessions'][session]['history'].append({'role':role,'content':text,
                        'timestamp':datetime.now(timezone.utc).isoformat()})
                    tmp = history_file.with_suffix('.tmp')
                    tmp.write_text(json.dumps(data)); tmp.replace(history_file)
            append('target','assistant','previous turn done')
            append('origin','user','origin still working')
            accepted, posts = [], []
            mode = {'target':'drop'}
            class Handler(BaseHTTPRequestHandler):
                def do_POST(self):
                    payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    posts.append(payload)
                    session = payload['session_key'].split(':')[1]
                    if mode.get(session) != 'drop':
                        append(session,'user',payload['prompt']); accepted.append(payload)
                    self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
                def log_message(self,*args): pass
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
            bindings={'cc_connect':{'codex-bot':{'session_key':'test:target',
                'webhook_port':server.server_port,'data_dir':str(root),'delivery_ack_timeout':.15}},'cdp':{}}
            (root/'bindings.json').write_text(json.dumps(bindings))
            (root/'registry.json').write_text(json.dumps({'version':2,'agents':{
                'codex-bot':{'type':'codex','provider':'cc_connect'}},'groups':{}}))
            sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
            env=dict(os.environ,CC_RELAY_HUB_DIR=str(root),CC_PROJECT='codex-bot',CC_SESSION_KEY='test:origin',
                HOOK_PORT=str(port),HOOK_EVENTS_FILE=str(root/'events.jsonl'),PYTHON_BIN=sys.executable,
                IDLE_DRAIN_DELAY_MS='50',STARTUP_DRAIN_DELAY_MS='20',
                RECONCILE_INTERVAL_SECS='0',OUTBOX_WATCHDOG_INTERVAL_SECS='0')
            log=(root/'hook.log').open('w')
            process=None
            def start_hook():
                return subprocess.Popen(['node',str(ROOT/'hook-server.mjs')],env=env,cwd=str(ROOT),
                    stdin=subprocess.DEVNULL,stdout=log,stderr=log)
            def post_hook(session, content):
                request=urllib.request.Request('http://127.0.0.1:%d/cc-connect/hooks/reply'%port,
                    data=json.dumps({'event':'message.sent','project':'codex-bot','session_key':'test:'+session,
                                     'content':content}).encode(),headers={'Content-Type':'application/json'})
                with urllib.request.urlopen(request,timeout=3) as response: self.assertEqual(response.status,204)
            def port_open():
                try:
                    with socket.create_connection(('127.0.0.1',port),timeout=.1): return True
                except OSError: return False
            def cli(*args):
                result=subprocess.run([sys.executable,str(ROOT/'hub.py'),*args],env=env,cwd=str(ROOT),
                    capture_output=True,text=True,timeout=15)
                self.assertEqual(result.returncode,0,result.stderr+result.stdout)
                return result.stdout
            try:
                # HTTP 200 is NOT an ACK. Send must persist, exit zero and keep ID.
                output=cli('send','codex-bot','T10 isolated request')
                self.assertIn('queued (target busy)',output)
                rid=re.search(r'id=([a-f0-9]+)',output).group(1)
                self.assertEqual(store.get_message(rid)['status'],'queued')
                self.assertEqual(len(posts),1)
                cli('send','codex-bot','T10 notice one','--no-reply')
                cli('send','codex-bot','T10 notice two','--no-reply')
                self.assertEqual(len(queue.rows()),3)
                self.assertEqual(json.loads(cli('queue','--format','json'))['targets']['codex-bot']['pending'],3)
                # A process restart with target now idle recovers persisted work.
                mode['target']='accept'
                append('target','assistant','idle after dropped webhook')
                process=start_hook();self.assertTrue(wait_until(port_open))
                # Startup honors retry backoff. A real idle hook bypasses it.
                post_hook('target','idle')
                self.assertTrue(wait_until(lambda: queue.get(rid)['status']=='delivered'), (root/'hook.log').read_text())
                self.assertEqual(len(accepted),1)
                self.assertIn('[cc-relay request_id=%s]'%rid,accepted[0]['prompt'])
                self.assertEqual(len(queue.rows()),2)
                # Fake target finishes; reply is saved for busy origin, notices
                # go to target in one FIFO turn, without any foreign identity.
                reply='[cc-relay reply_to=%s]\nT10 isolated reply'%rid
                append('target','assistant',reply);post_hook('target',reply)
                self.assertTrue(wait_until(lambda: store.get_message(rid)['status']=='replied'))
                self.assertTrue(wait_until(lambda: len(queue.rows())==0))
                self.assertEqual(len(store.list_notifications(status='open')),1)
                target_posts=[p for p in accepted if p['session_key']=='test:target']
                self.assertEqual(len(target_posts),2)
                self.assertLess(target_posts[1]['prompt'].index('notice one'),target_posts[1]['prompt'].index('notice two'))
                self.assertEqual([p for p in accepted if p['session_key']=='test:origin'],[])
                # Stop/restart again: no duplicate target delivery. Origin idle
                # releases its durable reply notification through the same hook.
                process.terminate();process.wait(5);process=start_hook();self.assertTrue(wait_until(port_open))
                append('origin','assistant','origin now idle');post_hook('origin','origin now idle')
                self.assertTrue(wait_until(lambda: store.get_message(rid)['notified_at'] is not None))
                origin_posts=[p for p in accepted if p['session_key']=='test:origin']
                self.assertEqual(len(origin_posts),1)
                self.assertIn('T10 isolated reply',origin_posts[0]['prompt'])
                self.assertEqual(len(accepted),3)
                self.assertTrue(all(p['project']=='codex-bot' for p in posts))
                print('T10 E2E: queued 3; target accepted request + FIFO notice batch; origin reply delivered once; restart passed.')
            finally:
                if process and process.poll() is None: process.terminate();process.wait(5)
                log.close()


if __name__=='__main__': unittest.main()
