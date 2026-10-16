"""Durable Grok reply reader, detached from the CLI that submitted the task.

The only CDP reader loop lives here. Asynchronous results use handle_hook_event
and the durable notification outbox, including busy-origin retries. One-shot
synchronous callers read the same stored result without an extra chat notice.
"""
from __future__ import annotations

import argparse
from contextvars import ContextVar
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from cdp.backends.base import CDPConfig
from cdp.backends.grok_bot import GrokBotBackend, GrokNotSentError, ReplyTracker, runtime_dir
from core.envelope import DeliveryReceipt


# Per-invocation adapter context, serialized into the job before spawning.
# Other CLI processes and asynchronous requests retain their normal behavior.
SYNC_REQUEST = ContextVar('grok_sync_request', default=None)


def save_job(path, job):
    temporary = path.with_suffix('.%s.tmp' % os.getpid())
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        json.dump(job, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    os.replace(str(temporary), str(path))


def job_path(request_id):
    return runtime_dir() / (hashlib.sha256(request_id.encode()).hexdigest() + '.json')


def timeout_for(binding):
    default = 2700 if binding.get('bot_name') == 'YouTube热度' else 1800
    return max(1, int(binding.get('response_timeout_secs', default)))


def make_backend(binding):
    return GrokBotBackend(CDPConfig(port=int(binding.get('cdp_port', 9333))), bot_name=binding['bot_name'])


def start_request(agent_id, binding, envelope):
    sync = SYNC_REQUEST.get()
    if sync is not None:
        binding = dict(binding, response_timeout_secs=sync['timeout'])
        sync['request_id'] = envelope.request_id
    started = time.time()
    path = job_path(envelope.request_id)
    backend = make_backend(binding)
    lease = None
    try:
        existing = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        if existing and existing.get('phase') == 'complete':
            return DeliveryReceipt(envelope.request_id, 'delivered', 'cdp', existing.get('sent_at', started))
        try:
            lease = backend.bot_lease()
        except RuntimeError:
            if existing and existing.get('phase') in ('waiting', 'result_ready'):
                return DeliveryReceipt(envelope.request_id, 'delivered', 'cdp', existing.get('sent_at', started))
            raise
        if existing and existing.get('phase') in ('waiting', 'result_ready'):
            job = existing  # recover a crashed reader, never send the task again
        else:
            job = existing or {'version': 1, 'agent_id': agent_id, 'binding': dict(binding),
                              'envelope': asdict(envelope), 'created_at': started}
            job['state_path'] = binding.get('_relay_state_path')
            if sync is not None:
                job['result_delivery'] = 'stdout'
            if existing and existing.get('phase') in ('sending', 'send_unconfirmed'):
                initial = backend.read_snapshot(request_id=envelope.request_id)
                anchor = '[cc-relay task_id=%s]' % envelope.request_id
                if not any(m['role'] == 'user' and anchor in m['text'] for m in initial['messages']):
                    raise RuntimeError('Send unconfirmed; no automatic resend without an observed task marker')
            else:
                job['phase'] = 'sending'
                save_job(path, job)
                initial = backend.begin(envelope.body, envelope.request_id)
            tracker = ReplyTracker(envelope.request_id, started, timeout=timeout_for(binding))
            tracker.observe(initial, time.time())
            job.update(phase='waiting', tracker=tracker.to_dict(), sent_at=time.time())
            save_job(path, job)
        log_path = path.with_suffix('.log')
        log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(log_fd, 'ab') as log:
            subprocess.Popen(
                [sys.executable, '-m', 'providers.grok_worker', '--job', str(path), '--lease-fd', str(lease.fd)],
                cwd=str(Path(__file__).resolve().parent.parent), stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, start_new_session=True, pass_fds=(lease.fd,),
            )
        # Do not overwrite the worker's checkpoint after spawning it.
        return DeliveryReceipt(envelope.request_id, 'delivered', 'cdp', job['sent_at'])
    except Exception as exc:
        if path.exists():
            existing = json.loads(path.read_text(encoding='utf-8'))
            if existing.get('phase') == 'sending':
                existing.update(phase='not_sent' if isinstance(exc, GrokNotSentError) else 'send_unconfirmed', error=str(exc))
                save_job(path, existing)
        return DeliveryReceipt(envelope.request_id, 'failed', 'cdp', started, str(exc))
    finally:
        backend.close()
        if lease:
            # Child inherited the same open-file description; no explicit unlock.
            lease.close()


def deliver_result(job):
    """Persist stdout-mode results; otherwise reuse the cc-connect hook/outbox."""
    import hub
    request_id = job['envelope']['request_id']
    if not job['envelope'].get('expect_reply', True):
        return {'status': 'notice_complete'}
    store = hub.StateStore(str(job.get('state_path') or hub.STATE_DB_PATH))
    # Parent marks the receipt delivered just after launching us.
    deadline = time.monotonic() + 10
    while True:
        message = store.get_message(request_id)
        if message and message.get('status') not in ('pending', 'queued'):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('Relay message was not committed as delivered; result retained for resume')
        time.sleep(0.1)
    if message.get('status') == 'failed':
        raise RuntimeError('Relay message failed; result retained for explicit recovery')
    if job.get('result_delivery') == 'stdout':
        # The existing --wait framework reads this same message row. A one-shot
        # caller consumes stdout, so do not also enqueue a later chat notice.
        if message.get('target') != job['agent_id']:
            raise RuntimeError('Synchronous Grok result target mismatch')
        if message.get('status') != 'replied' or message.get('reply_body') != job['result']:
            now = time.time()
            store.append_event('message.sync_reply', job['agent_id'], request_id,
                               message['session_key'], job['result'], now)
            store.mark_replied(request_id, job['result'], now)
        store.release_session_lock(message['session_key'], request_id)
        return {'status': 'stdout', 'request_id': request_id}
    result = hub.handle_hook_event({
        'event': 'message.sent', 'project': job['agent_id'],
        'session_key': message['session_key'],
        'content': '[cc-relay reply_to=%s]\n%s' % (request_id, job['result']),
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }, state_path=job.get('state_path'))
    if result.get('status') not in ('matched', 'duplicate', 'followup'):
        raise RuntimeError('Grok result not accepted by relay: %s' % result.get('status'))
    return result


def run_job(path, lease_fd=None):
    path = Path(path)
    job = json.loads(path.read_text(encoding='utf-8'))
    backend = make_backend(job['binding'])
    lease = None
    if lease_fd is None:
        lease = backend.bot_lease()
    try:
        if job.get('phase') == 'complete':
            return
        if job.get('phase') not in ('waiting', 'result_ready'):
            raise RuntimeError('Unconfirmed send: inspect Bot manually before recovery; refusing automatic resend')
        if job['phase'] == 'waiting':
            tracker = ReplyTracker.restore(job['tracker'])
            while True:
                now = time.time()
                if now >= tracker.deadline:
                    action = 'timeout'
                else:
                    try:
                        snapshot = backend.read_snapshot(request_id=tracker.request_id)
                        action = tracker.observe(snapshot, now)
                        job.pop('last_read_error', None)
                    except Exception as exc:
                        tracker.idle_since = None
                        job['last_read_error'] = str(exc)
                        action = 'wait'
                job['tracker'] = tracker.to_dict()
                save_job(path, job)
                if action == 'chase':
                    # Checkpoint chased=True BEFORE sending, including failed/uncertain sends.
                    try:
                        backend.chase(tracker.request_id)
                    except Exception as exc:
                        job['chase_error'] = str(exc)
                    save_job(path, job)
                elif action in ('complete', 'timeout'):
                    job.update(phase='result_ready', result=tracker.render(timeout=action == 'timeout'),
                               result_status=action, completed_at=time.time())
                    save_job(path, job)
                    break
                # No application lock or WebSocket is held during this wait.
                time.sleep(min(10, max(0.01, tracker.deadline - time.time())))
        job['delivery'] = deliver_result(job)
        job['phase'] = 'complete'
        job['notified_at'] = time.time()
        save_job(path, job)
        print(json.dumps({'request_id': job['envelope']['request_id'], 'status': job.get('result_status'),
                          'chars': len(job.get('result', '')), 'delivery': job['delivery'].get('status')}, ensure_ascii=False), flush=True)
    finally:
        backend.close()
        if lease:
            lease.close()
        if lease_fd is not None:
            os.close(lease_fd)
        if job.get('phase') == 'complete':
            # An idle edge for this Bot, after releasing its inherited task lock.
            # The same driver also handles missed edges on process/server restart.
            subprocess.Popen([sys.executable, str(Path(__file__).resolve().parents[1] / 'hub.py'),
                              '_drain_notifications', '--origin-project', job['agent_id'],
                              '--origin-session', 'cdp:' + job['agent_id'], '--force'],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', required=True, help='Saved Grok job JSON (resume without resending)')
    parser.add_argument('--lease-fd', type=int, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()
    run_job(args.job, args.lease_fd)


if __name__ == '__main__':
    main()
