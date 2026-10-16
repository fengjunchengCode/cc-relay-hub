import argparse
from contextlib import redirect_stdout
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import hub
from core.delivery_queue import DeliveryQueue, NOTICE_TTL, REQUEST_TTL, drain
from core.envelope import DeliveryReceipt, RelayEnvelope
from core.match import wait_for_reply_framework
from core.state import StateStore
from providers.cc_connect import CCConnectProvider
from providers.grok_worker import SYNC_REQUEST, start_request
from cdp.backends.grok_bot import GrokNotSentError


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'state.db'
        self.store = StateStore(str(self.db))
        self.queue = DeliveryQueue(self.store)
        self.binding = {'session_key': 'test:target', 'webhook_port': 1, 'data_dir': str(self.root)}
        self.agent = {'name': 'codex-bot', 'provider': 'cc_connect', 'binding': self.binding}
        self.bindings = {'cc_connect': {'codex-bot': self.binding}}
        self.provider = MagicMock()
        self.provider.supports_control.return_value = False
        self.provider.has_delivery_marker.return_value = False
        self.provider.get_session_activity.return_value = {'status': 'busy'}
        self.provider.deliver.side_effect = lambda e: DeliveryReceipt(e.request_id, 'delivered', 'cc_connect', time.time())
        for name, value in [('STATE_DB_PATH', self.db), ('HUB_DIR', self.root),
                            ('BINDINGS_PATH', self.root/'bindings.json'), ('REGISTRY_PATH', self.root/'registry.json')]:
            p = patch.object(hub, name, value); p.start(); self.addCleanup(p.stop)
        p = patch.object(hub, 'get_provider', return_value=self.provider); p.start(); self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'CC_RELAY_HUB_DIR': str(self.root)}); p.start(); self.addCleanup(p.stop)

    def send(self, body='question', reply=True, rid=None, agent=None):
        return hub.deliver_to_local_agent(agent or self.agent, body, expect_reply=reply,
            request_id=rid, state=self.store, origin_project='codex-bot', origin_session='test:origin')

    def run_drain(self, **kwargs):
        return drain(hub, state_path=self.db, bindings=self.bindings, **kwargs)

    def test_busy_notice_and_request_persist_with_distinct_ttls_and_original_origin(self):
        for reply, ttl in [(False, NOTICE_TTL), (True, REQUEST_TTL)]:
            result = self.send(reply=reply)
            self.assertEqual(result['status'], 'queued')
            item = self.queue.get(result['request_id'])
            self.assertEqual(item['expires_at'] - item['created_at'], ttl)
            message = self.store.get_message(result['request_id'])
            self.assertEqual((message['status'], message['origin_project'], message['origin_session_key']),
                             ('queued', 'codex-bot', 'test:origin'))
        self.provider.deliver.assert_not_called()

    def test_fifo_and_adjacent_notices_batch_without_crossing_request(self):
        ids = [self.send(str(i), reply=r)['request_id'] for i, r in enumerate([False, False, True, False])]
        self.provider.get_session_activity.return_value = {'status': 'idle'}
        self.run_drain(force=True)
        self.assertEqual(self.provider.deliver.call_count, 1)
        envelope = self.provider.deliver.call_args.args[0]
        self.assertEqual(envelope.request_id, ids[0])
        self.assertIn('[cc-relay notice_id=%s]' % ids[1], envelope.body)
        self.assertNotIn(ids[2], envelope.body)
        self.assertEqual([r['request_id'] for r in self.queue.rows()], ids[2:])
        self.run_drain(force=True)
        self.assertEqual(self.provider.deliver.call_args.args[0].request_id, ids[2])
        self.assertEqual([r['request_id'] for r in self.queue.rows()], [ids[3]])

    def test_http_failure_and_timeout_retry_restart_then_late_ack_dedup(self):
        self.provider.get_session_activity.return_value = {'status': 'idle'}
        self.provider.deliver.side_effect = TimeoutError('webhook timeout')
        result = self.send()
        item = self.queue.get(result['request_id'])
        self.assertEqual((result['status'], item['attempts']), ('queued', 1))
        self.assertIn('timeout', item['last_error'])
        # Fresh process/store representation; ACK arrives after original window.
        self.queue = DeliveryQueue(StateStore(str(self.db)))
        self.provider.has_delivery_marker.return_value = True
        self.run_drain(force=True)
        self.assertEqual(self.provider.deliver.call_count, 1)
        self.assertEqual(self.queue.get(result['request_id'])['status'], 'delivered')

    def test_two_concurrent_drainers_only_one_transport_attempt(self):
        result = self.send()
        self.provider.get_session_activity.return_value = {'status': 'idle'}
        entered, release = threading.Event(), threading.Event()
        def send(envelope):
            entered.set(); self.assertTrue(release.wait(3))
            return DeliveryReceipt(envelope.request_id, 'delivered', 'cc_connect', time.time())
        self.provider.deliver.side_effect = send
        errors = []
        def first():
            try: self.run_drain(force=True)
            except Exception as exc: errors.append(exc)
        thread = threading.Thread(target=first); thread.start()
        try:
            self.assertTrue(entered.wait(3))
            self.run_drain(force=True)
            self.assertEqual(self.provider.deliver.call_count, 1)
        finally:
            release.set(); thread.join(3)
        self.assertFalse(errors)
        self.assertEqual(self.queue.get(result['request_id'])['status'], 'delivered')

    def test_expired_notice_and_request_enqueue_one_notification_and_do_not_deliver(self):
        results = [self.send(reply=False), self.send(reply=True)]
        now = time.time()
        self.run_drain(now=now + NOTICE_TTL + 1)
        self.assertEqual(self.queue.get(results[0]['request_id'])['status'], 'expired')
        self.assertEqual(self.queue.get(results[1]['request_id'])['status'], 'pending')
        self.run_drain(now=now + REQUEST_TTL + 1)
        self.run_drain(now=now + REQUEST_TTL + 2)
        notices = self.store.list_notifications()
        self.assertEqual(len(notices), 2)
        self.assertTrue(all('[EXPIRED]' in r['content'] for r in notices))
        self.assertTrue(all(r['origin_project'] == 'codex-bot' for r in notices))
        self.provider.deliver.assert_not_called()

    def test_reply_to_queued_id_keeps_origin_and_settles_after_ack_crash(self):
        result = self.send()
        payload = {'event': 'message.sent', 'project': 'codex-bot', 'session_key': 'test:target',
                   'content': '[cc-relay reply_to=%s]\nnew result' % result['request_id']}
        with patch.object(hub, 'drain_notification_outbox', return_value=[]):
            reply = hub.handle_hook_event(payload, state_path=self.db, bindings=self.bindings)
        self.assertEqual(reply['status'], 'matched')
        self.run_drain(force=True)
        message = self.store.get_message(result['request_id'])
        self.assertEqual((message['status'], message['reply_body']), ('replied', 'new result'))
        self.assertEqual(self.queue.get(result['request_id'])['status'], 'delivered')
        self.assertEqual(self.store.list_notifications()[0]['origin_session_key'], 'test:origin')
        self.provider.deliver.assert_not_called()

    def test_wait_deadline_does_not_cancel_queued_request(self):
        result = self.send()
        self.assertIsNone(wait_for_reply_framework(self.store, self.provider, result['request_id'], 'test:target', .01, .01))
        self.assertEqual(self.store.get_message(result['request_id'])['status'], 'queued')
        self.provider.poll_events.assert_not_called()

    def test_same_request_id_is_idempotent_and_no_replied_row_replaced(self):
        self.send(rid='same')
        self.store.mark_replied('same', 'reply', time.time())
        self.send(rid='same')
        self.assertEqual(self.store.get_message('same')['reply_body'], 'reply')
        self.assertEqual(len(self.queue.rows(include_closed=True)), 1)

    def test_stale_dispatch_lease_and_sending_row_recover(self):
        self.send(rid='crash')
        key = json.dumps(['dispatch', 'codex-bot', 'test:target'])
        with self.store._connect() as conn:
            conn.execute("UPDATE delivery_queue SET status='sending'")
            conn.execute('INSERT INTO delivery_leases VALUES (?,?,?)', (key, 'dead-process', 0))
        self.provider.get_session_activity.return_value = {'status': 'idle'}
        self.run_drain(force=True)
        self.assertEqual(self.queue.get('crash')['status'], 'delivered')

    def test_cli_queued_exit_zero_and_queue_summary_no_message_body(self):
        args = hub.parse_args(['send','codex-bot','secret body','--no-reply'])
        registry = {'agents': {'codex-bot': {'provider': 'cc_connect', 'type': 'codex'}}, 'groups': {}}
        output = io.StringIO()
        with patch.object(hub, 'ensure_registry_and_bindings', return_value=(registry, self.bindings)), \
             patch.object(hub, 'resolve_origin_context', return_value=('codex-bot','test:origin')), \
             patch.object(hub, 'origin_group_chat_name', return_value=None), redirect_stdout(output):
            self.assertEqual(hub.cmd_send(args), 0)
        self.assertIn('queued (target busy)', output.getvalue())
        self.assertIn('id=', output.getvalue())
        output = io.StringIO()
        with redirect_stdout(output): self.assertEqual(hub.main(['queue','--format','json']), 0)
        view = json.loads(output.getvalue())
        self.assertEqual(view['targets']['codex-bot']['pending'], 1)
        self.assertNotIn('secret body', output.getvalue())

    def test_grok_busy_queues_and_sync_options_survive_retry(self):
        agent = {'name': 'test-grok', 'provider': 'cdp', 'binding': {'backend':'grok_bot','bot_name':'测试'}}
        self.provider.deliver.side_effect = lambda e: DeliveryReceipt(e.request_id,'failed','cdp',time.time(),'Grok operation busy')
        context = {'timeout': 7}
        token = SYNC_REQUEST.set(context)
        try: result = self.send(agent=agent)
        finally: SYNC_REQUEST.reset(token)
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(context['request_id'], result['request_id'])
        seen = []
        def accept(envelope):
            seen.append(dict(SYNC_REQUEST.get()))
            return DeliveryReceipt(envelope.request_id,'delivered','cdp',time.time())
        self.provider.deliver.side_effect = accept
        drain(hub, state_path=self.db, bindings={'cdp': {'test-grok':agent['binding']}}, force=True)
        self.assertEqual(seen[0]['timeout'], 7)
        self.assertIsNone(SYNC_REQUEST.get())

    def test_grok_definitely_not_sent_can_retry_same_id_uncertain_send_cannot(self):
        backend = MagicMock(); backend.bot_lease.return_value.fd = 88
        path = self.root/'job.json'
        envelope = RelayEnvelope('grok-test','codex-bot','test-grok','test',time.time(),None)
        snapshot = {'messages': [{'role':'user','text':'[cc-relay task_id=grok-test]'}], 'busy': False}
        with patch('providers.grok_worker.job_path', return_value=path), \
             patch('providers.grok_worker.make_backend', return_value=backend), \
             patch('providers.grok_worker.subprocess.Popen'):
            backend.begin.side_effect = GrokNotSentError('busy')
            self.assertEqual(start_request('test-grok', {'bot_name':'测试'},envelope).status,'failed')
            self.assertEqual(json.loads(path.read_text())['phase'],'not_sent')
            backend.begin.side_effect = None; backend.begin.return_value = snapshot
            self.assertEqual(start_request('test-grok', {'bot_name':'测试'},envelope).status,'delivered')
            job=json.loads(path.read_text());job['phase']='send_unconfirmed';path.write_text(json.dumps(job))
            backend.read_snapshot.return_value = {'messages':[], 'busy':False}
            backend.begin.reset_mock()
            self.assertEqual(start_request('test-grok', {'bot_name':'测试'},envelope).status,'failed')
            backend.begin.assert_not_called()
            backend.read_snapshot.return_value=snapshot
            self.assertEqual(start_request('test-grok', {'bot_name':'测试'},envelope).status,'delivered')
            backend.begin.assert_not_called()

    def test_late_ack_wins_over_expiry_and_notification_enqueue_recovers(self):
        self.send(rid='late')
        self.provider.has_delivery_marker.return_value = True
        self.run_drain(now=time.time()+REQUEST_TTL+1)
        self.assertEqual(self.queue.get('late')['status'],'delivered')
        self.assertFalse(self.store.list_notifications())
        self.provider.has_delivery_marker.return_value = False
        self.send(rid='expires')
        with patch.object(hub,'enqueue_reply_notification',side_effect=OSError('temporary')):
            with self.assertRaises(OSError): self.run_drain(now=time.time()+REQUEST_TTL+1)
        self.assertEqual(self.queue.get('expires')['status'],'expired')
        self.run_drain(now=time.time()+REQUEST_TTL+1)
        self.assertEqual(len(self.store.list_notifications()),1)

    def test_unreadable_history_is_durable_and_late_delivery_stays_reconcilable(self):
        self.provider.get_session_activity.side_effect = ValueError('partial history')
        result = self.send()
        self.assertEqual(result['status'],'queued')
        self.assertIn('unreadable',self.queue.get(result['request_id'])['last_error'])
        with self.store._connect() as conn:
            conn.execute('UPDATE messages SET created_at=?',(time.time()-REQUEST_TTL,))
        self.store.mark_delivered(result['request_id'],time.time())
        self.assertEqual(self.store.list_open_messages(max_age_secs=3600)[0]['request_id'],result['request_id'])

    def test_remote_queued_receipt_keeps_tracking_without_false_failure(self):
        from noded import NodeDaemon
        from core.peer_protocol import new_envelope
        self.store.insert_message('remote','codex-bot','test-target','remote:test','remote','question','pending',time.time())
        envelope=new_envelope('request','fake-local','fake-remote',{'request_id':'remote'})
        self.store.enqueue_peer_envelope(envelope)
        daemon=NodeDaemon.__new__(NodeDaemon);daemon.store=self.store
        receipt=new_envelope('receipt','fake-remote','fake-local',{'ref':envelope['envelope_id'],
            'request_id':'remote','status':'queued','error':'target busy'})
        daemon._handle_receipt(receipt,'test')
        self.assertEqual(self.store.get_message('remote')['status'],'queued')
        self.assertEqual(self.store.get_peer_outbox(envelope['envelope_id'])['status'],'delivered')

    def test_sync_queue_wait_timeout_returns_id_and_timeout_exit_status(self):
        import grok_sync
        self.send(rid='sync-queued')
        def send(args):
            SYNC_REQUEST.get()['request_id']='sync-queued'
            return 1
        with patch.object(grok_sync,'resolve_grok',return_value={'binding':{}}), \
             patch.object(hub,'cmd_send',side_effect=send), \
             patch.object(grok_sync,'saved_result',return_value=None):
            code,status,body=grok_sync.run(grok_sync.parse_args(['test-grok','question']))
        self.assertEqual((code,status),(1,'TIMEOUT'))
        self.assertIn('sync-queued',body)
        self.assertEqual(self.store.get_message('sync-queued')['status'],'queued')

    def test_app_lock_failure_is_definitely_not_sent_and_provider_read_timeout_is_queued(self):
        from cdp.backends.grok_bot import GrokBotBackend
        from cdp.backends.base import CDPConfig
        backend=GrokBotBackend(CDPConfig(port=1),bot_name='测试')
        with patch.object(backend,'ensure_running'), \
             patch.object(backend,'app_lock',side_effect=RuntimeError('application busy')), \
             patch.object(backend,'_send_text') as send:
            with self.assertRaises(GrokNotSentError): backend.begin('question','test')
            send.assert_not_called()
        self.provider.get_session_activity.return_value={'status':'idle'}
        actual=CCConnectProvider('codex-bot',dict(self.binding,_relay_state_path=str(self.db)))
        with patch.object(hub,'get_provider',return_value=actual), \
             patch('providers.cc_connect.urllib.request.urlopen',side_effect=TimeoutError('socket read timeout')):
            result=self.send()
        self.assertEqual(result['status'],'queued')
        self.assertIn('socket read timeout',self.queue.get(result['request_id'])['last_error'])

    def test_transport_mutex_shared_and_activity_scoped_to_bound_session(self):
        path = self.root/'sessions';path.mkdir()
        data = {'active_session': {'test:target':'s1','test:other':'s2'},
                'user_sessions': {'test:target':['s1'],'test:other':['s2']}, 'sessions':{
                    's1':{'history':[{'role':'assistant','content':'done','timestamp':'2026-10-16T00:00:00Z'}]},
                    's2':{'history':[{'role':'user','content':'[cc-relay request_id=foreign]','timestamp':'2026-10-16T00:01:00Z'}]}}}
        (path/'codex-bot_test.json').write_text(json.dumps(data))
        provider = CCConnectProvider('codex-bot', dict(self.binding,_relay_state_path=str(self.db)))
        self.assertEqual(provider.get_session_activity()['status'], 'idle')
        self.assertFalse(provider.has_delivery_marker('foreign',True))
        envelope=RelayEnvelope('test','source','codex-bot','question',time.time(),None)
        with self.queue.lease('transport','codex-bot','test:target') as acquired:
            self.assertTrue(acquired)
            with patch.object(provider,'_deliver_unlocked') as send:
                self.assertIn('transport',provider.deliver(envelope).error)
                send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
