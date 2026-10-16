"""Grok completion, virtualization, process locks, and asynchronous relay tests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import require_sandbox
require_sandbox()

from cdp.backends.grok_bot import (FileLease, GrokBotBackend, GrokConnection,
                                   ReplyTracker, acknowledgement_only, added_since)
from core.envelope import RelayEnvelope
from providers.cdp_provider import CDPProvider
from providers.grok_worker import deliver_result, start_request, timeout_for


def msg(role, text, images=0):
    return {'role': role, 'text': text, 'images': images}


def question(request='r1'):
    return msg('user', '任务\n[cc-relay task_id=%s]' % request)


def snap(messages, busy=False):
    return {'name': '总管', 'messages': messages, 'busy': busy, 'draft': ''}


class TrackerTest(unittest.TestCase):
    def test_long_split_reply_is_complete_without_truncation(self):
        tracker = ReplyTracker('r1', 0)
        old = msg('assistant', 'OLD CONTENT MUST NOT LEAK')
        first, second = '甲' * 6200, '乙' * 5800
        self.assertEqual(tracker.observe(snap([old, question(), msg('assistant', first)], True), 1), 'wait')
        self.assertEqual(tracker.observe(snap([question(), msg('assistant', first), msg('assistant', second)]), 10), 'wait')
        self.assertEqual(tracker.observe(snap([question(), msg('assistant', first), msg('assistant', second)]), 69), 'wait')
        self.assertEqual(tracker.observe(snap([question(), msg('assistant', first), msg('assistant', second)]), 70), 'complete')
        self.assertEqual(tracker.render(), first + '\n\n' + second)

    def test_busy_resets_quiet_window_even_when_text_is_unchanged(self):
        t = ReplyTracker('r1', 0)
        messages = [question(), msg('assistant', '测试通过')]
        t.observe(snap(messages), 0)
        self.assertEqual(t.observe(snap(messages, True), 80), 'wait')
        self.assertEqual(t.observe(snap(messages), 90), 'wait')
        self.assertEqual(t.observe(snap(messages), 149), 'wait')
        self.assertEqual(t.observe(snap(messages), 150), 'complete')

    def test_acknowledgement_chased_once_then_collects_result(self):
        t = ReplyTracker('r1', 0)
        messages = [question(), msg('assistant', '收到，我先查一下，完成后发你。')]
        t.observe(snap(messages), 1)
        self.assertEqual(t.observe(snap(messages), 180), 'wait')
        self.assertEqual(t.observe(snap(messages), 181), 'chase')
        t = ReplyTracker.restore(t.to_dict())
        self.assertEqual(t.observe(snap(messages), 500), 'wait')
        messages += [msg('user', '请把完整结果一次发出来\n[cc-relay followup_id=r1]'), msg('assistant', '实际结果' * 200)]
        self.assertEqual(t.observe(snap(messages), 510), 'wait')
        self.assertEqual(t.observe(snap(messages), 570), 'complete')
        self.assertEqual(len(t.replies()), 2)

    def test_timeout_keeps_partial_text_and_image_count(self):
        t = ReplyTracker('r1', 0, timeout=100)
        t.observe(snap([question(), msg('assistant', '部分结果', 3)], True), 2)
        self.assertEqual(t.observe(snap([question(), msg('assistant', '部分结果', 3)], True), 100), 'timeout')
        text = t.render(timeout=True)
        self.assertTrue(text.startswith('[TIMEOUT]'))
        self.assertIn('部分结果\n[图片 3 张，未取回]', text)

    def test_timeout_without_anchor_does_not_claim_old_answers(self):
        t = ReplyTracker('r1', 0, timeout=100)
        self.assertEqual(t.observe(snap([question('other'), msg('assistant', 'old answer')]), 100), 'timeout')
        self.assertNotIn('old answer', t.render(True))

    def test_virtualized_question_and_earlier_chunks_are_preserved(self):
        t = ReplyTracker('r1', 0)
        t.observe(snap([question(), msg('assistant', 'part 1'), msg('assistant', 'part 2')], True), 1)
        t.observe(snap([msg('assistant', 'part 2'), msg('assistant', 'part 3')], True), 10)
        t.observe(snap([msg('assistant', 'part 3 grows')]), 20)
        self.assertEqual(t.replies(), ['part 1', 'part 2', 'part 3 grows'])
        self.assertEqual(t.observe(snap([msg('assistant', 'part 3 grows')]), 80), 'complete')

    def test_unrelated_window_is_not_added_after_virtualization(self):
        t = ReplyTracker('r1', 0)
        t.observe(snap([question(), msg('assistant', 'ours')]), 1)
        t.observe(snap([msg('assistant', 'unrelated')]), 20)
        self.assertEqual(t.replies(), ['ours'])
        self.assertEqual(t.observe(snap([msg('assistant', 'unrelated')]), 90), 'wait')
        self.assertEqual(t.observe(snap([msg('assistant', 'unrelated')]), 1800), 'timeout')

    def test_foreign_user_message_stops_at_boundary(self):
        t = ReplyTracker('r1', 0)
        action = t.observe(snap([question(), msg('assistant', 'ours'), msg('user', 'someone else'), msg('assistant', 'theirs')]), 10)
        self.assertEqual(action, 'timeout')
        self.assertIn('ours', t.render(True))
        self.assertNotIn('theirs', t.render(True))

    def test_image_only_message_is_a_real_answer(self):
        t = ReplyTracker('r1', 0)
        t.observe(snap([question(), msg('assistant', '', 1)]), 1)
        t.observe(snap([msg('assistant', '', 2)]), 10)
        self.assertEqual(t.observe(snap([msg('assistant', '', 2)]), 70), 'complete')
        self.assertEqual(t.render(), '[图片 2 张，未取回]')

    def test_new_chunks_restart_quiet_window(self):
        t = ReplyTracker('r1', 0)
        t.observe(snap([question(), msg('assistant', 'first')]), 1)
        messages = [question(), msg('assistant', 'first'), msg('assistant', 'late chunk')]
        self.assertEqual(t.observe(snap(messages), 59), 'wait')
        self.assertEqual(t.observe(snap(messages), 61), 'wait')
        self.assertEqual(t.observe(snap(messages), 119), 'complete')

    def test_ack_classifier_and_overlap(self):
        for value in ['收到', '好的', 'Got it', "I'll check and send the report"]:
            self.assertTrue(acknowledgement_only([value]), value)
        self.assertFalse(acknowledgement_only(['测试通过']))
        self.assertFalse(acknowledgement_only(['收到数据：' + '详细内容' * 100]))
        a, b, c = msg('user', 'a'), msg('assistant', 'b'), msg('assistant', 'c')
        self.assertEqual(added_since([a, b], [b, c]), [c])


class TransportTest(unittest.TestCase):
    def test_response_id_matching_ignores_unsolicited_cdp_events(self):
        ws = MagicMock()
        ws.recv.side_effect = [json.dumps({'method': 'Page.event'}), json.dumps({'id': 99, 'result': {}}), json.dumps({'id': 1, 'result': {'result': {'value': 'right'}}})]
        # Exercise our transport with a fake websocket module; the optional
        # live-CDP dependency must not come from the real user's site-packages.
        websocket = types.ModuleType('websocket')
        websocket.create_connection = MagicMock(return_value=ws)
        with patch.dict(sys.modules, {'websocket': websocket}):
            conn = GrokConnection('ws://127.0.0.1/fake')
            self.assertEqual(conn.evaluate('example'), 'right')

    @unittest.skipUnless(os.name == 'posix', 'Grok Bot application locks require macOS/POSIX')
    def test_application_lock_excludes_other_process_but_releases(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'app.lock'
            code = 'from cdp.backends.grok_bot import FileLease; import sys; FileLease(sys.argv[1],timeout=0).close()'
            with FileLease(path):
                result = subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
            result = subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True)
            self.assertEqual(result.returncode, 0)

    @unittest.skipUnless(os.name == 'posix', 'Grok Bot application locks require macOS/POSIX')
    def test_different_bot_leases_do_not_serialize_waits(self):
        with tempfile.TemporaryDirectory() as folder, patch('cdp.backends.grok_bot.runtime_dir', return_value=Path(folder)):
            one, two = GrokBotBackend(bot_name='X热度'), GrokBotBackend(bot_name='竞品站')
            with one.bot_lease(), two.bot_lease():
                with one.app_lock():
                    pass
                with two.app_lock():
                    pass
                with self.assertRaises(RuntimeError):
                    one.bot_lease()

    @unittest.skipUnless(os.name == 'posix', 'Grok Bot application locks require macOS/POSIX')
    def test_bot_lease_survives_parent_closing_inherited_fd(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'bot.lock'
            lease = FileLease(path, timeout=0)
            child = subprocess.Popen([sys.executable, '-c',
                'import sys; print("ready",flush=True); sys.stdin.readline()'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                pass_fds=(lease.fd,))
            try:
                self.assertEqual(child.stdout.readline().strip(), 'ready')
                lease.close()
                with self.assertRaises(RuntimeError):
                    FileLease(path, timeout=0)
                child.communicate('\n', timeout=5)
                FileLease(path, timeout=0).close()
            finally:
                lease.close()
                if child.poll() is None:
                    child.kill()
                child.communicate()

    def test_busy_bot_and_unsent_draft_are_not_overwritten(self):
        b = GrokBotBackend()
        for state in [dict(busy=True, draft=''), dict(busy=False, draft='human draft')]:
            with patch.object(b, '_snapshot', return_value=state), patch.object(b, 'ensure_conn') as conn:
                with self.assertRaises(RuntimeError):
                    b._send_text('new task')
                conn.assert_not_called()

    def test_virtual_dom_scan_recovers_disjoint_chunks_and_grouped_articles(self):
        b = GrokBotBackend()
        def indexed(message, row, part=0):
            return dict(message, row_index=row, part_index=part)
        q = indexed(question(), 90)
        first = indexed(msg('assistant', 'first'), 91)
        second = indexed(msg('assistant', 'second'), 91, 1)
        third = indexed(msg('assistant', 'third'), 92)
        fourth = indexed(msg('assistant', 'fourth'), 93)
        conn = MagicMock()
        conn.evaluate.side_effect = [None, True, True, None]
        with patch.object(b, 'ensure_conn', return_value=conn), patch('cdp.backends.grok_bot.time.sleep'), \
                patch.object(b, '_snapshot', side_effect=[snap([fourth]), snap([third, fourth]), snap([q, first, second, third])]):
            result = b._collect_to_anchor('[cc-relay task_id=r1]')
        self.assertEqual(result['messages'], [q, first, second, third, fourth])

    def test_virtual_dom_scan_missing_anchor_cannot_claim_complete(self):
        b = GrokBotBackend()
        conn = MagicMock()
        conn.evaluate.side_effect = [None, False, None]
        other = dict(msg('assistant', 'unrelated'), row_index=2, part_index=0)
        with patch.object(b, 'ensure_conn', return_value=conn), patch('cdp.backends.grok_bot.time.sleep'), \
                patch.object(b, '_snapshot', return_value=snap([other])):
            with self.assertRaisesRegex(RuntimeError, 'anchor is absent'):
                b._collect_to_anchor('[cc-relay task_id=r1]')

    def test_auto_start_uses_requested_app_and_loopback_port(self):
        b = GrokBotBackend()
        with patch.object(b, 'app_lock') as lock, patch.object(b, 'is_running', side_effect=[(False, ''), (True, '')]), patch('cdp.backends.grok_bot.subprocess.run') as run:
            b.ensure_running()
            self.assertEqual(run.call_args[0][0], ['open', '-a', 'Grok Bot', '--args', '--remote-debugging-port=9333'])

    def test_timeout_defaults(self):
        self.assertEqual(timeout_for({'bot_name': '总管'}), 1800)
        self.assertEqual(timeout_for({'bot_name': 'YouTube热度'}), 2700)

    def test_provider_starts_detached_reader_for_async_send(self):
        e = RelayEnvelope('test', 'origin', 'grok-admin', '请只回复：测试通过', 0, None)
        with patch('providers.grok_worker.start_request', return_value='receipt') as start:
            p = CDPProvider('grok-admin', {'backend': 'grok_bot', 'bot_name': '总管'})
            self.assertEqual(p.deliver(e), 'receipt')
            start.assert_called_once_with('grok-admin', p.binding, e)
            self.assertFalse(p.supports_control())

    def test_finished_result_uses_original_origin_hook_and_full_body(self):
        import hub
        body = '长' * 13000
        job = {'agent_id': 'grok-admin', 'envelope': {'request_id': 'abc', 'expect_reply': True}, 'result': body}
        with patch.object(hub, 'StateStore') as store, patch.object(hub, 'handle_hook_event', return_value={'status': 'matched'}) as hook:
            store.return_value.get_message.return_value = {'status': 'delivered', 'session_key': 'cdp:grok-admin'}
            self.assertEqual(deliver_result(job)['status'], 'matched')
            payload = hook.call_args[0][0]
            self.assertEqual(payload['content'], '[cc-relay reply_to=abc]\n' + body)
            self.assertEqual(payload['session_key'], 'cdp:grok-admin')

    def test_real_hook_accepts_timestamp_and_enqueues_full_reply_once(self):
        import hub
        from core.state import StateStore
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'state.db'
            store = StateStore(str(db))
            store.insert_message('real-hook', 'origin', 'grok-admin', 'cdp:grok-admin',
                                 'cdp', 'task', 'delivered', 0,
                                 origin_project='test-origin', origin_session_key='test:origin')
            body = '完整内容' * 2000
            job = {'agent_id': 'grok-admin', 'envelope': {'request_id': 'real-hook'}, 'result': body}
            with patch.object(hub, 'STATE_DB_PATH', db), patch.object(hub, 'drain_notification_outbox'):
                self.assertEqual(deliver_result(job)['status'], 'matched')
                self.assertEqual(deliver_result(job)['status'], 'duplicate')
            message = store.get_message('real-hook')
            self.assertEqual(message['status'], 'replied')
            self.assertEqual(message['reply_body'], body)
            outbox = store.list_notifications(status='pending')
            self.assertEqual(len(outbox), 1)
            self.assertEqual(outbox[0]['origin_project'], 'test-origin')
            self.assertEqual(outbox[0]['origin_session_key'], 'test:origin')
            self.assertIn(body, outbox[0]['content'])


if __name__ == '__main__':
    unittest.main()
