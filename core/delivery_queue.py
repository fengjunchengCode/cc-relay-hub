"""Durable local send queue. SQLite transactions precede every external side effect.

Dispatch leases serialize a destination across processes; transport leases also
serialize send() with the reply outbox. A stable marker is checked before every
retry. Delivery is at-least-once if the receiver never persists an ACK; it cannot
be exactly-once without receiver-side idempotency.
"""
from contextlib import contextmanager
from dataclasses import asdict, replace
import json
import time
import uuid

from core.envelope import RelayEnvelope
from core.relay_protocol import build_relay_prompt

NOTICE_TTL = 24 * 3600
REQUEST_TTL = 72 * 3600


class DeliveryQueue:
    def __init__(self, store):
        self.store = store
        with store._connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS delivery_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    target TEXT NOT NULL, session_key TEXT NOT NULL,
                    provider TEXT NOT NULL, expect_reply INTEGER NOT NULL,
                    envelope TEXT NOT NULL, options TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL, expires_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL, last_attempt_at REAL,
                    delivered_at REAL, last_error TEXT,
                    expiry_notified INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS delivery_queue_fifo
                    ON delivery_queue(target, session_key, status, id);
                CREATE TABLE IF NOT EXISTS delivery_leases (
                    destination TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
            ''')

    @contextmanager
    def lease(self, scope, target, session, seconds=180):
        key = json.dumps([scope, target, session])
        owner, now = uuid.uuid4().hex, time.time()
        with self.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('DELETE FROM delivery_leases WHERE destination=? AND expires_at<=?', (key, now))
            result = conn.execute('INSERT OR IGNORE INTO delivery_leases VALUES (?,?,?)',
                                  (key, owner, now + seconds))
            acquired = result.rowcount == 1
        try:
            yield acquired
        finally:
            if acquired:
                with self.store._connect() as conn:
                    conn.execute('DELETE FROM delivery_leases WHERE destination=? AND owner=?', (key, owner))

    def enqueue(self, agent, envelope, origin_project=None, origin_session=None,
                origin_node=None, envelope_id=None, options=None):
        session = agent['binding'].get('session_key') or 'cdp:' + agent['name']
        now = time.time()
        expires = now + (REQUEST_TTL if envelope.expect_reply else NOTICE_TTL)
        with self.store._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            # Never replace a replied message on a duplicate inbound envelope.
            conn.execute('''INSERT OR IGNORE INTO messages
                (request_id,sender,target,session_key,provider,body,status,created_at,
                 origin_project,origin_session_key,direction,origin_node,envelope_id)
                VALUES (?,?,?,?,?,?,'queued',?,?,?,?,?,?)''',
                (envelope.request_id, envelope.sender, envelope.target, session,
                 agent['provider'], envelope.body, now, origin_project, origin_session,
                 'inbound' if origin_node else 'local', origin_node, envelope_id))
            conn.execute('''INSERT OR IGNORE INTO delivery_queue
                (request_id,target,session_key,provider,expect_reply,envelope,options,
                 created_at,expires_at,next_attempt_at) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (envelope.request_id, envelope.target, session, agent['provider'],
                 int(envelope.expect_reply), json.dumps(asdict(envelope), ensure_ascii=False),
                 json.dumps(options or {}), now, expires, now))
        return self.get(envelope.request_id)

    def get(self, request_id):
        with self.store._connect() as conn:
            row = conn.execute('SELECT * FROM delivery_queue WHERE request_id=?', (request_id,)).fetchone()
            return dict(row) if row else None

    def rows(self, target=None, session=None, include_closed=False):
        clauses, params = [], []
        if not include_closed:
            clauses.append("status IN ('pending','sending')")
        for name, value in [('target', target), ('session_key', session)]:
            if value:
                clauses.append(name + '=?'); params.append(value)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self.store._connect() as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM delivery_queue' + where + ' ORDER BY id', params)]

    def delivered(self, item, now):
        with self.store._connect() as conn:
            conn.execute("UPDATE delivery_queue SET status='delivered',delivered_at=?,last_error=NULL WHERE id=?",
                         (now, item['id']))
            conn.execute("UPDATE messages SET status='delivered',delivered_at=?,error=NULL WHERE request_id=? AND status IN ('queued','pending')",
                         (now, item['request_id']))

    def defer(self, items, reason, now, attempted=False):
        with self.store._connect() as conn:
            for item in items:
                delay = min(300, 5 * 2 ** min(item['attempts'], 6)) if attempted else 5
                conn.execute("UPDATE delivery_queue SET status='pending',last_error=?,next_attempt_at=? WHERE id=?",
                             (reason, now + delay, item['id']))

    def expire(self, item):
        body = '[EXPIRED] 消息未能在 %s 小时内投递给 %s，已停止重试（id=%s）。请确认目标恢复后重新发送。' % (
            72 if item['expect_reply'] else 24, item['target'], item['request_id'])
        with self.store._connect() as conn:
            conn.execute("UPDATE delivery_queue SET status='expired',last_error=? WHERE id=?", (body, item['id']))
            conn.execute("UPDATE messages SET status='expired',error=?,reply_body=? WHERE request_id=? AND status='queued'",
                         (body, body, item['request_id']))

    def notify_expired(self, notify):
        with self.store._connect() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM delivery_queue WHERE status='expired' AND expiry_notified=0")]
        for item in rows:
            message = self.store.get_message(item['request_id'])
            # Idempotent notification_outbox enqueue happens BEFORE marking done.
            if json.loads(item['options']).get('sync') or notify(message, item['last_error']):
                with self.store._connect() as conn:
                    conn.execute('UPDATE delivery_queue SET expiry_notified=1 WHERE id=?', (item['id'],))

    def _provider(self, item, bindings, factory):
        binding = bindings.get(item['provider'], {}).get(item['target'])
        if not binding:
            return None
        binding = dict(binding, _relay_state_path=str(self.store.db_path))
        if item['provider'] == 'cc_connect':
            binding['session_key'] = item['session_key']  # pinned destination, never a newly bound chat
        return factory({'name': item['target'], 'provider': item['provider'], 'binding': binding})

    def drain(self, bindings, factory, target=None, session=None, force=False, now=None, limit=100):
        now = time.time() if now is None else now
        destinations = list(dict.fromkeys((r['target'], r['session_key']) for r in self.rows(target, session)))
        results = []
        for agent, key in destinations[:limit]:
            with self.lease('dispatch', agent, key) as acquired:
                if not acquired:
                    continue
                rows = self.rows(agent, key)
                pending = []
                for item in rows:
                    provider = self._provider(item, bindings, factory)
                    message = self.store.get_message(item['request_id'])
                    # Late ACK / reply after a process crash settles without re-POST.
                    ack = message.get('status') in ('delivered', 'replied')
                    if provider and item['provider'] == 'cc_connect':
                        ack = ack or provider.has_delivery_marker(item['request_id'], bool(item['expect_reply']))
                    if ack:
                        self.delivered(item, now)
                    elif item['expires_at'] <= now:
                        self.expire(item)
                    else:
                        pending.append(item)
                if not pending:
                    continue
                first = pending[0]
                if not force and first['next_attempt_at'] > now:
                    continue
                provider = self._provider(first, bindings, factory)
                if provider is None:
                    self.defer([first], 'target binding missing', now)
                    continue
                if first['provider'] == 'cc_connect':
                    try:
                        activity = provider.get_session_activity()
                    except (OSError, ValueError) as exc:
                        self.defer([first], 'session history unreadable: %s' % exc, now)
                        continue
                    if activity.get('status') == 'busy':
                        self.defer([first], 'target busy', now)
                        continue
                # Coalesce only an adjacent notice prefix; never cross a request.
                batch = [first]
                if first['provider'] == 'cc_connect' and not first['expect_reply']:
                    size = len(first['envelope'])
                    for item in pending[1:20]:
                        if item['expect_reply'] or size + len(item['envelope']) > 64000:
                            break
                        batch.append(item); size += len(item['envelope'])
                envelope = RelayEnvelope(**json.loads(first['envelope']))
                if len(batch) > 1:
                    envelope = replace(envelope, body='以下通知按入队顺序合并投递：\n\n' + '\n\n---\n\n'.join(
                        build_relay_prompt(RelayEnvelope(**json.loads(item['envelope']))) for item in batch))
                with self.store._connect() as conn:
                    for item in batch:
                        conn.execute("UPDATE delivery_queue SET status='sending',attempts=attempts+1,last_attempt_at=? WHERE id=?", (now, item['id']))
                try:
                    # Synchronous Grok options survive a queued/restarted dispatch.
                    from providers.grok_worker import SYNC_REQUEST
                    options = json.loads(first['options'])
                    token = SYNC_REQUEST.set(options.get('sync'))
                    try:
                        receipt = provider.deliver(envelope)
                    finally:
                        SYNC_REQUEST.reset(token)
                    if receipt.status == 'delivered':
                        for item in batch:
                            self.delivered(item, receipt.delivered_at)
                        results.append({'status': 'delivered', 'target': agent, 'request_id': first['request_id'], 'batch_size': len(batch)})
                    else:
                        self.defer(batch, receipt.error or 'delivery unconfirmed', now, attempted=True)
                except Exception as exc:
                    self.defer(batch, str(exc), now, attempted=True)
                # Always return after ONE confirmed turn per destination. Its next
                # final/idle edge (or watchdog) is what releases the next request.
        return results


def supported(agent):
    return agent['provider'] == 'cc_connect' or (
        agent['provider'] == 'cdp' and agent['binding'].get('backend') == 'grok_bot')


def submit(hub, agent, envelope, state, **origin):
    from providers.grok_worker import SYNC_REQUEST
    sync = SYNC_REQUEST.get()
    if sync is not None:
        sync['request_id'] = envelope.request_id
    queue = DeliveryQueue(state)
    queue.enqueue(agent, envelope, options={'sync': dict(sync)} if sync is not None else {}, **origin)
    bindings = {agent['provider']: {agent['name']: agent['binding']}}
    try:
        queue.drain(bindings, hub.get_provider, target=agent['name'],
                    session=agent['binding'].get('session_key') or 'cdp:' + agent['name'])
    except Exception as exc:
        # Persistence already succeeded; a transient dispatcher/read failure
        # must not tell a caller to manually resend the same durable task.
        item = queue.get(envelope.request_id)
        if item['status'] in ('pending', 'sending'):
            queue.defer([item], str(exc), time.time())
    item = queue.get(envelope.request_id)
    return {'status': 'delivered' if item['status'] == 'delivered' else 'queued',
            'request_id': envelope.request_id, 'session_key': item['session_key'],
            'created_at': item['created_at'], 'delivered_at': item['delivered_at'],
            'error': item['last_error'] or 'target busy', 'expires_at': item['expires_at']}


def drain(hub, state_path=None, bindings=None, target=None, session=None, force=False, limit=100, now=None):
    store = hub.StateStore(str(state_path or hub.STATE_DB_PATH))
    queue = DeliveryQueue(store)
    results = queue.drain(hub.load_bindings(bindings), hub.get_provider, target, session, force, now, limit)
    def notify(message, body):
        if message.get('origin_node') and message['origin_node'] != hub.local_node_id():
            # Existing cross-node outbox handles remote expiry notices too.
            hub.forward_reply_to_origin_node(store, message, body, label='expired')
            return True
        return bool(hub.enqueue_reply_notification(store, message, body, label='expired'))
    queue.notify_expired(notify)
    return results


def show(hub, args):
    queue = DeliveryQueue(hub.StateStore(str(hub.STATE_DB_PATH)))
    rows = queue.rows(target=args.target, include_closed=args.all)
    now, groups = time.time(), {}
    public = []
    for row in rows:
        item = {key: row[key] for key in ('request_id','target','session_key','status','created_at','expires_at','attempts','last_error')}
        item['age_seconds'] = max(0, round(now - row['created_at'], 1))
        public.append(item)
        if row['status'] in ('pending','sending'):
            group = groups.setdefault(row['target'], {'pending': 0, 'oldest_age_seconds': 0})
            group['pending'] += 1
            group['oldest_age_seconds'] = max(group['oldest_age_seconds'], item['age_seconds'])
    if args.format == 'json':
        print(json.dumps({'targets': groups, 'items': public}, ensure_ascii=False))
    else:
        for name, group in groups.items():
            print('%s: %d queued; oldest %.0fs' % (name, group['pending'], group['oldest_age_seconds']))
        for item in public:
            print('%s id=%s target=%s age=%.0fs attempts=%d reason=%s' % (
                item['status'], item['request_id'], item['target'], item['age_seconds'], item['attempts'], item['last_error'] or '-'))
        if not public:
            print('No queued deliveries.')
    return 0
