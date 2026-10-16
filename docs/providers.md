# Providers

cc-relay-hub currently supports two provider families: `cc_connect` and `cdp`.

| Provider | Transport | Reply detection | Best fit |
| --- | --- | --- | --- |
| `cc_connect` | Local HTTP webhook | `message.sent` hook server | Claude Code, Codex, Gemini CLI, or any agent already bridged by cc-connect |
| `cdp` | Chrome DevTools Protocol WebSocket | IDE relay markers; Grok Bot uses new assistant messages and an idle window | Electron IDEs and Grok Bot with local CDP enabled |

## cc-connect Provider

The cc-connect provider sends a message to a local cc-connect webhook. Replies come back through the relay hook server.

Example registry entry:

```json
{
  "version": 2,
  "agents": {
    "codex-bot": {
      "type": "codex",
      "provider": "cc_connect",
      "work_dir": "/path/to/workdir",
      "capabilities": ["message.send", "history.read", "session.control"],
      "labels": []
    }
  }
}
```

Example binding:

```json
{
  "cc_connect": {
    "codex-bot": {
      "config_path": "/path/to/.cc-connect/config-codex.toml",
      "webhook_host": "127.0.0.1",
      "webhook_port": 9112,
      "webhook_path": "/hook",
      "session_key": "feishu:chat_id:user_id"
    }
  }
}
```

## CDP Provider

The CDP provider controls an Electron IDE through Chrome DevTools Protocol. It can type a relay prompt into the IDE, wait for the answer, and extract the relay marker from the visible transcript.

Example registry entry:

```json
{
  "agents": {
    "antigravity-ide": {
      "type": "antigravity",
      "provider": "cdp",
      "work_dir": "/path/to/project",
      "capabilities": ["message.send", "session.control"],
      "labels": ["ide"]
    }
  }
}
```

Example binding:

```json
{
  "cdp": {
    "antigravity-ide": {
      "backend": "antigravity",
      "cdp_port": 9000
    }
  }
}
```

Agents use the same request path for CDP targets as for webhook targets:

```bash
cc-relay-hub info antigravity-ide
cc-relay-hub cdp status antigravity-ide
cc-relay-hub send antigravity-ide "Open the project and inspect the failing workflow"
```

For CDP agents, `Session` may be empty and `Last Seen` may remain `never`; this is expected because replies are read from the IDE DOM transcript rather than from the hook server. Use `send --wait` only for explicit synchronous diagnostics. If a diagnostic wait times out, inspect the IDE state before retrying:

```bash
cc-relay-hub cdp probe antigravity-ide
cc-relay-hub cdp heal antigravity-ide
cc-relay-hub cdp screenshot antigravity-ide --path /tmp/antigravity.png
```

## CDP Safety

CDP can control the target IDE window. Keep debugging ports bound to `127.0.0.1`, do not expose them to a network, and close the IDE debugging session when not needed.

## Grok Bot (macOS)

The `grok_bot` CDP backend addresses each sidebar Bot as a separate relay agent.
It starts `/Applications/Grok Bot.app` with `open -a "Grok Bot" --args
--remote-debugging-port=9333` when CDP is unavailable. An already running app
that was started without debugging may require a normal manual restart; the
relay never kills the app. Python needs `websocket-client`, as for the existing
CDP backend. It neither logs in nor changes the application.

Back up `registry.json` and `bindings.json` before registration. Merge an agent
entry into the registry and add its name to the appropriate group's `members`:

```json
{
  "grok-admin": {
    "type": "grok_bot",
    "provider": "cdp",
    "work_dir": "/path/to/project",
    "capabilities": ["message.send"],
    "labels": ["grok", "bot"]
  }
}
```

Merge the corresponding entry into `bindings.json` under `cdp`:

```json
{
  "grok-admin": {
    "backend": "grok_bot",
    "cdp_port": 9333,
    "bot_name": "总管",
    "response_timeout_secs": 1800
  }
}
```

The deployed `fjc-group` mappings are:

| Agent | Sidebar Bot | Reply deadline |
| --- | --- | --- |
| `grok-competitor` | 竞品站 | 30 minutes |
| `grok-x` | X热度 | 30 minutes |
| `grok-tiktok` | TikTok热度 | 30 minutes |
| `grok-youtube` | YouTube热度 | 45 minutes |
| `grok-outreach` | 外链 | 30 minutes |
| `grok-admin` | 总管 | 30 minutes |

```bash
cc-relay-hub list
cc-relay-hub info grok-admin
cc-relay-hub cdp status grok-admin
cc-relay-hub send grok-admin "请只回复：测试通过"
```

Run `send` in the initiating agent's normal cc-connect environment so
`CC_PROJECT` and `CC_SESSION_KEY` identify the real return conversation. No
`--wait`, listener, or caller-side CDP script is necessary. The CLI returns after
confirming that the user message was inserted; a detached provider reader
collects the answer. It wraps the answer in the matching relay reply marker and
passes it to the existing hook handler and durable origin-notification outbox.
Grok itself does not need to emit a reply marker. The binding's
`response_timeout_secs` controls the reader deadline, independently of the CLI's
synchronous `--wait` timeout.

An application-wide file lock serializes Bot switching, sending, and snapshot
reads. It is released while answers are being generated, so different Bots can
run concurrently. A second lease prevents two relay tasks from using the same
Bot at once. A busy Bot or a nonempty unsent draft is left untouched and `send`
reports `queued (target busy)` with a durable ID; it retries after the Bot finishes
or the recovery watchdog runs. A draft is never overwritten. The updated `bin/grok_ask.mjs` wrapper shares these locks;
older standalone CDP copies and the deprecated `grok_to.mjs` / `grok_tail.mjs`
do not. Manual interaction during a task can
change its destination; use an idle Bot for other work.

The reader takes **all assistant messages after the unique current user-message
anchor**, including multiple articles in one message block. It retains earlier
chunks if the DOM virtualizes them away. When the task anchor has left the DOM,
the reader scrolls the observed transcript back to the question, collects rows
by their visible `data-index` and article position, then returns to the bottom.
This also recovers split chunks that arrived and left the viewport between two
reads. A scan that cannot reach the question cannot count as a successful
completion. No whole-page tail or output-length limit is used. Busy indicators
are scoped to the current chat so another Bot's work notification does not
block completion. Completion
requires both no busy indicator and 60 seconds without new reply content. If
only short acknowledgement messages remain idle for three minutes, one follow-up
asks for the complete result. A deadline or another user's message returns
`[TIMEOUT]` followed by any already collected content. Images are represented as
`[图片 N 张，未取回]`; image bytes are not downloaded.

`info` and `cdp status` report CDP availability, whether the app is running, the
requested Bot, the currently displayed Bot, and that displayed Bot's busy state.
Health checks do not switch Bots or start the application.

Reader checkpoints and logs are private files under
`$CC_RELAY_HUB_DIR/.cc-relay-hub/grok/` (default
`~/.cc-connect/cc-relay-hub/.cc-relay-hub/grok/`). Job filenames are the SHA-256
of the request ID; checkpoints contain the task and full reply and must not be
committed. Logs contain status and character counts, not reply text. If a reader
process exits unexpectedly, resume an existing `waiting` or `result_ready` job
from the repository root:

```bash
python3 -m providers.grok_worker --job /absolute/path/to/job.json
```

Resume never sends the original task again. A live reader's Bot lease prevents
duplicate resume. A `send_unconfirmed` checkpoint requires inspecting the Bot
and relay message state before recovery; it is deliberately not retried
automatically. Once a result is accepted by the hook, the existing outbox handles
delivery retries when the origin is busy. A machine/app shutdown can delay or
prevent observation; the deadline still returns any preserved partial answer.

### Synchronous calls for one-shot jobs

The existing `send --wait` can read Grok replies from the shared message store,
but prints raw text, retains asynchronous notifications, and has a wait timeout
separate from the Bot reader. Use the dedicated adapter for scheduled jobs:

```bash
cc-relay-hub grok-sync grok-x --timeout 1800 "Research this keyword"
cc-relay-hub grok-sync grok-admin --message-file /absolute/path/question.txt
cc-relay-hub grok-sync --bot-name 总管 --message-file /absolute/path/question.txt
```

This local macOS entry point reuses `hub.cmd_send` for group, origin and private
channel checks, `wait_for_reply_framework` for waiting, and the **same detached
Grok reader, Bot lease, app lock, message collection and completion rules** as
asynchronous sends. `--timeout` sets this request's reader deadline in seconds
(default: 1800; YouTube: 2700). The CLI allows up to 30 additional seconds to
finish a bounded DOM read and persist the result. Binding defaults are not
changed. Different Bots can run synchronous and asynchronous work concurrently.

Standard output contains only a result envelope:

```text
OK
The complete new reply, including all split messages.
```

Timeouts start with `TIMEOUT`, followed by the preserved partial reply and its
`[TIMEOUT]` explanation. Exit status is 0 for OK, 1 for timeout, or 2 for a
send/routing error (a `TIMEOUT` envelope containing `[ERROR]`, also reported on
stderr). Invalid CLI syntax uses the usual argparse error on stderr. Synchronous
results are persisted in the message store and job checkpoint, but **do not also
enqueue a chat notification**. This lets a scheduled one-shot session consume
the result immediately. Interrupted callers can inspect/resume the saved job;
the original question is never automatically resent.

The compatibility wrapper keeps its four positional parameters:

```bash
node /path/to/grok_ask.mjs 总管 /path/question.txt /path/answer.txt 15
```

It calls `grok-sync --bot-name`, writes exactly the stdout envelope to the output
file, echoes it on stdout, and propagates the exit code. Its optional timeout
remains **minutes, default 15**, preserving the old interface. Bot names must
already be registered (no hidden direct-CDP fallback). File paths and questions
are passed as argument arrays/file input, without a shell or output-buffer cap.
The versioned copy is `bin/grok_ask.mjs`; the project copy under `工具/cdp/`
is identical. `CC_RELAY_HUB_BIN` may select a different installed relay executable.

## Backend Status

| Backend | Status | Notes |
| --- | :---: | --- |
| Antigravity | ✅ | Primary CDP backend |
| Cursor | ⚠️ | Stub/backend work in progress |
| Grok Bot | ✅ | Per-Bot routing, full multi-message replies, detached asynchronous reader |
| Other Electron IDEs | ⚠️ | Requires a backend that can locate the chat input and transcript |

## Durable outgoing queue (cc-connect and Grok)

Normal `send` requests and `send --no-reply` notices are committed to both
`messages` and `delivery_queue` **before** attempting the webhook/CDP operation.
Busy sessions, webhook failures, timeouts and missing ACKs return exit 0:

```text
queued (target busy): AGENT (id=REQUEST_ID expires_at=UNIX_SECONDS); REASON
```

This means accepted by the hub, not yet accepted by the target. Do not resend.
Group/privacy validation and invalid bindings still fail before delivery. Slash
control commands retain their existing direct-control semantics.

```bash
cc-relay-hub queue
cc-relay-hub queue --target AGENT
cc-relay-hub queue --all --format json
```

JSON has `targets[AGENT].pending`, `oldest_age_seconds`, and `items` with
`request_id`, `target`, `session_key`, `status`, `created_at`, `expires_at`,
`age_seconds`, `attempts`, `last_error`. It excludes task content. `pending` and
`sending` are open; `delivered` and `expired` are closed. `inbox` continues to show
**return** notifications, not outgoing tasks.

An authoritative `message.sent`, `session.idle` or `turn.final` hook schedules
the existing delayed drain. Each destination sends one request or a contiguous
batch of up to 20 notices (64 KB stored envelopes) at a time, in FIFO order.
Requests are never combined. A later item cannot bypass a failed oldest item.
Per-destination SQLite leases serialize dispatchers; a separate transport lease
is shared with the reply outbox. Busy/ACK checks use the bound cc-connect session,
not a different chat's history. A stable request/notice marker settles late ACKs
before another POST. Startup and the existing 300-second watchdog recover missed
idle edges and transient network failures (5–300 second retry backoff). They do
not probe known-busy cc-connect sessions. A Grok worker releases its per-Bot lock
and triggers the same drain on completion. No caller-side wait loop is needed.

Notices expire after 24 hours, requests after 72 hours **in the delivery queue**.
The next drain marks an unaccepted item `expired` and puts an `[EXPIRED]` notice
in the existing origin notification outbox. The return notice itself can wait for
the origin to become idle. An observed late ACK takes precedence over expiration.
Accepted tasks are not expired by the delivery TTL. `--timeout` is a synchronous
wait/response limit, not the delivery TTL; ending a wait does not cancel a queued
request or lose its origin/reply ID. Grok sync retains its stdout-only result
contract, including queued TIMEOUT IDs; later results stay in its checkpoint.

Ambiguous Grok sends are never blindly repeated: recovery looks for the original
user task anchor, then resumes the existing reader. Only a definitive rejection
before input/Enter can be automatically resent. If an ACK is never persisted by
cc-connect, exactly-once delivery cannot be guaranteed; this is durable retry
with stable IDs and observed-marker deduplication, not a receiver transaction.

Tests: `python3.11 -B tests/run_isolated.py` uses a fresh interpreter and temporary
HOME, registry and DB, and checks production file fingerprints afterwards;
`python3.11 -B tests/run_isolated.py tests.test_delivery_queue tests.test_delivery_queue_e2e`
runs the queue acceptance subset. The end-to-end test uses a local simulated
webhook and a separate hook server, both sessions named codex-bot, and verifies
busy → durable queue → idle → FIFO delivery → reply outbox → origin delivery.
Never use another real agent's identity or production DB for tests. See
[Testing safely](testing.md) for the required runner and isolation guarantees.
