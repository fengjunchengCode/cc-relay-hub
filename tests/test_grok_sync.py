import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import require_sandbox
require_sandbox()

import grok_sync
import hub
from core.envelope import RelayEnvelope
from core.match import wait_for_reply_framework
from core.state import StateStore
from providers.grok_worker import SYNC_REQUEST, deliver_result, start_request


class GrokSyncTest(unittest.TestCase):
    def test_interspersed_args_and_bot_name(self):
        args = grok_sync.parse_args(['grok-admin', '--timeout', '1800', 'question'])
        self.assertEqual((args.agent, args.message, args.timeout), ('grok-admin', 'question', 1800))
        args = grok_sync.parse_args(['--bot-name', '总管', '--message-file', '/tmp/q.txt'])
        self.assertEqual((args.bot_name, args.message_file), ('总管', '/tmp/q.txt'))

    def test_worker_deadline_and_stdout_mode_are_per_request(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'job.json'
            backend = MagicMock()
            backend.bot_lease.return_value.fd = 77
            backend.begin.return_value = {'messages': [
                {'role': 'user', 'text': '[cc-relay task_id=sync-test]'}], 'busy': False}
            context = {'timeout': 7}
            token = SYNC_REQUEST.set(context)
            try:
                with patch('providers.grok_worker.job_path', return_value=path), \
                        patch('providers.grok_worker.make_backend', return_value=backend), \
                        patch('providers.grok_worker.subprocess.Popen'):
                    receipt = start_request('grok-admin', {'bot_name': '总管', 'response_timeout_secs': 1800},
                        RelayEnvelope('sync-test', 'origin', 'grok-admin', 'question', time.time(), None, ttl=37))
            finally:
                SYNC_REQUEST.reset(token)
            self.assertEqual(receipt.status, 'delivered')
            job = json.loads(path.read_text())
            self.assertEqual(job['result_delivery'], 'stdout')
            self.assertEqual(job['tracker']['deadline'] - job['created_at'], 7)
            self.assertEqual(context['request_id'], 'sync-test')
            self.assertIsNone(SYNC_REQUEST.get())

    def test_existing_wait_reads_sync_result_without_async_notice(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'state.db'
            store = StateStore(str(db))
            store.insert_message('sync-test', 'origin', 'grok-admin', 'cdp:grok-admin', 'cdp',
                                 'question', 'delivered', time.time(),
                                 origin_project='source', origin_session_key='feishu:source')
            body = '完整结果' * 2000
            job = {'agent_id': 'grok-admin', 'envelope': {'request_id': 'sync-test'},
                   'result_delivery': 'stdout', 'result': body}
            with patch.object(hub, 'STATE_DB_PATH', db), patch.object(hub, 'handle_hook_event') as hook:
                deliver_result(job)
                deliver_result(job)
                hook.assert_not_called()
            provider = MagicMock()
            actual = wait_for_reply_framework(store, provider, 'sync-test', 'cdp:grok-admin', 1, .01)
            self.assertEqual(actual, body)
            provider.poll_events.assert_not_called()
            self.assertEqual(store.list_notifications(status='open'), [])
            with store._connect() as conn:
                self.assertEqual(conn.execute('select count(*) from events').fetchone()[0], 1)

    def test_stdout_contract_complete_timeout_and_partial_fallback(self):
        for code, job, expected in [
            (0, {'result_status': 'complete', 'result': 'new reply'}, (0, 'OK', 'new reply')),
            (0, {'result_status': 'timeout', 'result': '[TIMEOUT] partial'}, (1, 'TIMEOUT', '[TIMEOUT] partial')),
            (1, None, (2, 'TIMEOUT', '[ERROR] send denied')),
        ]:
            args = grok_sync.parse_args(['grok-admin', 'question', '--timeout', '7'])
            def send(value):
                self.assertEqual(value.timeout, 37)
                self.assertEqual(SYNC_REQUEST.get()['timeout'], 7)
                print('send denied' if code else 'new reply')
                return code
            with patch.object(grok_sync, 'resolve_grok', return_value={'binding': {}}), \
                    patch.object(hub, 'cmd_send', side_effect=send), \
                    patch.object(grok_sync, 'saved_result', return_value=job):
                self.assertEqual(grok_sync.run(args), expected)
            self.assertIsNone(SYNC_REQUEST.get())
        tracker = grok_sync.ReplyTracker('sync-test', 0)
        tracker.observe({'messages': [{'role': 'user', 'text': tracker.anchor},
                                     {'role': 'assistant', 'text': 'saved partial'}]}, 1)
        with patch.object(grok_sync, 'resolve_grok', return_value={'binding': {}}), \
                patch.object(hub, 'cmd_send', return_value=1), \
                patch.object(grok_sync, 'saved_result', return_value={'tracker': tracker.to_dict()}):
            code, status, body = grok_sync.run(grok_sync.parse_args(['grok-admin', 'question']))
            self.assertEqual((code, status), (1, 'TIMEOUT'))
            self.assertIn('saved partial', body)

    def test_group_guards_are_not_bypassed(self):
        registry = {'version': 2, 'agents': {
            'source': {'type': 'codex', 'provider': 'cc_connect'},
            'grok-admin': {'type': 'grok_bot', 'provider': 'cdp'}},
            'groups': {'one': {'members': ['source']}, 'two': {'members': ['grok-admin']}}}
        bindings = {'cc_connect': {'source': {}}, 'cdp': {'grok-admin': {
            'backend': 'grok_bot', 'bot_name': '总管'}}}
        with patch.object(hub, 'ensure_registry_and_bindings', return_value=(registry, bindings)), \
                patch.object(hub, 'resolve_origin_context', return_value=('source', 'test:origin')), \
                patch.object(hub, 'origin_group_chat_name', return_value=None), \
                patch.object(hub, 'deliver_to_local_agent') as send:
            code, status, body = grok_sync.run(grok_sync.parse_args(['grok-admin', 'question']))
            self.assertEqual((code, status), (2, 'TIMEOUT'))
            self.assertIn('cross-group send blocked', body)
            send.assert_not_called()

    def test_non_grok_and_unregistered_bot_names_fail_before_send(self):
        with patch.object(hub, 'ensure_registry_and_bindings', return_value=({}, {'cdp': {}})):
            with self.assertRaisesRegex(ValueError, 'registered'):
                grok_sync.resolve_grok(grok_sync.parse_args(['--bot-name', 'missing', '--stdin']))
        with patch.object(hub, 'ensure_registry_and_bindings', return_value=({}, {})), \
                patch.object(hub, 'resolve_agent', return_value={'provider': 'cc_connect'}):
            with self.assertRaisesRegex(ValueError, 'local registered'):
                grok_sync.resolve_grok(grok_sync.parse_args(['agent', 'question']))


@unittest.skipUnless(shutil.which('node') and os.name == 'posix', 'Node/POSIX needed for compatibility wrapper tests')
class WrapperTest(unittest.TestCase):
    def test_wrapper_preserves_large_unicode_text_and_literal_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            question = folder / 'question $(no-command) with spaces.txt'
            question.write_text('问题正文')
            output = folder / 'sub folder' / 'answer.txt'
            fake = folder / 'fake relay'
            record = folder / 'args.json'
            fake.write_text('#!/usr/bin/env python3\nimport json,sys\n'
                            'from pathlib import Path\n'
                            'Path(%r).write_text(json.dumps(sys.argv[1:]))\n' % str(record)
                            + 'print("OK\\n"+"长"*400000)\n')
            fake.chmod(0o700)
            result = subprocess.run(['node', str(ROOT/'bin/grok_ask.mjs'), '总管', str(question), str(output), '2.5'],
                env=dict(os.environ, CC_RELAY_HUB_BIN=str(fake)), capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_text(), result.stdout)
            self.assertEqual(result.stdout, 'OK\n'+'长'*400000+'\n')
            self.assertEqual(json.loads(record.read_text()), ['grok-sync', '--bot-name', '总管',
                            '--message-file', str(question), '--timeout', '150'])

    def test_wrapper_retains_partial_timeout_and_overwrites_stale_output(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            question, output, fake = folder/'q', folder/'a', folder/'fake'
            question.write_text('question'); output.write_text('OK\nstale answer')
            fake.write_text('#!/bin/sh\nprintf "TIMEOUT\\npartial reply\\n"\nexit 1\n');fake.chmod(0o700)
            result = subprocess.run(['node', str(ROOT/'bin/grok_ask.mjs'), '总管', str(question), str(output)],
                env=dict(os.environ, CC_RELAY_HUB_BIN=str(fake)), capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(output.read_text(), 'TIMEOUT\npartial reply\n')
            question.write_text('')
            result = subprocess.run(['node', str(ROOT/'bin/grok_ask.mjs'), '总管', str(question), str(output)],
                env=dict(os.environ, CC_RELAY_HUB_BIN=str(fake)), capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(output.read_text(), 'TIMEOUT\n[ERROR] 问题文件为空\n')


if __name__ == '__main__':
    unittest.main()
