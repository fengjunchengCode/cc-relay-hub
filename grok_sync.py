"""One-shot Grok adapter: existing send guards + reader, stable stdout contract."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import io
import json
import os
import sys

import hub
from cdp.backends.grok_bot import ReplyTracker
from providers.grok_worker import SYNC_REQUEST, job_path, timeout_for


def parse_args(argv):
    parser = argparse.ArgumentParser(prog='cc-relay-hub grok-sync',
        description='Wait for a local registered Grok Bot. stdout starts with OK or TIMEOUT.')
    parser.add_argument('agent', nargs='?', help='Registered Grok agent name')
    parser.add_argument('--bot-name', help='Resolve a registered Bot by its Chinese sidebar name')
    hub._add_message_input_args(parser)
    parser.add_argument('--timeout', type=int, help='Reader deadline in seconds; defaults to the Bot binding')
    parser.add_argument('--group', default=None)
    parser.add_argument('--private', action='store_true')
    parser.add_argument('--origin-project', default=None, help=argparse.SUPPRESS)
    parser.add_argument('--origin-session', default=None, help=argparse.SUPPRESS)
    args = parser.parse_intermixed_args(argv)
    if bool(args.agent) == bool(args.bot_name):
        parser.error('provide exactly one of AGENT or --bot-name')
    if args.timeout is not None and args.timeout <= 0:
        parser.error('--timeout must be positive')
    args.wait, args.no_reply, args.no_queue = True, False, True
    return args


def resolve_grok(args):
    registry, bindings = hub.ensure_registry_and_bindings()
    origin, _ = hub.resolve_origin_context(args)
    if args.bot_name:
        matches = [name for name, binding in bindings.get('cdp', {}).items()
                   if binding.get('backend') == 'grok_bot' and binding.get('bot_name') == args.bot_name
                   and name in registry.get('agents', {})]
        if len(matches) != 1:
            raise ValueError('Bot name must match exactly one registered Grok agent: %s' % args.bot_name)
        args.agent = matches[0]
    agent = hub.resolve_agent(registry, bindings, args.agent,
                              sender=origin or os.environ.get('CC_PROJECT') or '', group=args.group)
    if agent.get('remote') or agent.get('provider') != 'cdp' or agent['binding'].get('backend') != 'grok_bot':
        raise ValueError('grok-sync requires a local registered grok_bot agent')
    args.agent = agent['name']
    return agent


def saved_result(context):
    if not context.get('request_id'):
        return None
    path = job_path(context['request_id'])
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def run(args):
    captured = io.StringIO()
    context = {}
    try:
        with redirect_stdout(captured):
            agent = resolve_grok(args)
            context['timeout'] = args.timeout or timeout_for(agent['binding'])
            # The worker's own deadline produces a partial TIMEOUT reply. Allow
            # one bounded DOM read + checkpoint/IPC flush after that deadline.
            args.timeout = context['timeout'] + 30
            token = SYNC_REQUEST.set(context)
            try:
                code = hub.cmd_send(args)  # includes all origin/group/privacy checks
            finally:
                SYNC_REQUEST.reset(token)
        job = saved_result(context)
        if code == 0 and job and job.get('result_status') in ('complete', 'timeout'):
            status = 'OK' if job['result_status'] == 'complete' else 'TIMEOUT'
            return (0 if status == 'OK' else 1), status, job['result']
        if job and job.get('tracker'):
            return 1, 'TIMEOUT', ReplyTracker.restore(job['tracker']).render(
                timeout=True, reason='同步等待结束；以下是检查点中已有的本次回复。')
        return 2, 'TIMEOUT', '[ERROR] ' + (captured.getvalue().strip() or 'Grok send failed')
    except Exception as exc:
        return 2, 'TIMEOUT', '[ERROR] %s' % exc


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    code, status, body = run(args)
    print(status)
    print(body)
    if code == 2:
        print(body, file=sys.stderr)
    return code


if __name__ == '__main__':
    sys.exit(main())
