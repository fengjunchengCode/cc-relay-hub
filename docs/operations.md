# Operations

This document covers runtime notes that are too detailed for the README.

## Bootstrap

After adding a new cc-connect instance, run:

```bash
cc-relay-hub bootstrap
```

This scans local cc-connect config files, writes registry/bindings, verifies connectivity, and installs cc-relay-hub context blocks into agent memory files.

Context bootstrap mirrors cc-connect's memory-file model:

- global memory files such as `~/.codex/AGENTS.md` and `~/.claude/CLAUDE.md`
- each configured agent `work_dir`
- cc-connect `work_dir_override` and `dir_history.json` directories created by `/dir`

Existing project instruction files are preserved. cc-relay-hub only updates the block between `<!-- cc-relay-hub:begin -->` and `<!-- cc-relay-hub:end -->`.

## cc-connect Webhook Setup

Every participating cc-connect config needs webhook delivery and a reply hook. The AI-agent installer in `INSTALL.md` can add this automatically.

Manual example:

```toml
[webhook]
enabled = true
port = 9110
path = "/hook"

[[hooks]]
event = "message.sent"
type = "http"
url = "http://127.0.0.1:9120/cc-connect/hooks/reply"
async = false
timeout = 2
```

Use a unique webhook port for each running cc-connect process.

## Hook Server

The hook server receives cc-connect `message.sent` events and exposes a long-poll endpoint for event watching.

```bash
node ~/.cc-connect/cc-relay-hub/hook-server.mjs
```

Default bind address:

```text
127.0.0.1:9120
```

## Session Initialization

`Session` should not be `none` in `cc-relay-hub list`. If it is empty, open that bot's own chat window, send one normal message, then run:

```bash
cc-relay-hub list
```

## Stale Claude Code Sessions

If a Claude Code backed cc-connect bot starts returning `(empty response)` or `(空响应)` immediately after a cc-connect restart, check the cc-connect log for:

```text
No conversation found with session ID
```

That means cc-connect is trying to resume a Claude Code conversation ID that Claude no longer has. Start a fresh chat session for that bot with `/new`, or clear the stale `agent_session_id` from the bot's active session file under `~/.cc-connect/sessions/` and restart that cc-connect instance.

## Waiting for Events

Do not use shell polling loops such as `tail -f`, `while true`, or repeated `sleep` commands. Use:

```bash
cc-relay-hub send <agent> "task" --wait
cc-relay-hub watch
cc-relay-hub watch --loop
```

## CDP Operations

Keep CDP debugging ports local to `127.0.0.1` and close them when not needed.

Useful commands:

```bash
cc-relay-hub cdp status <agent>
cc-relay-hub cdp screenshot <agent>
cc-relay-hub cdp probe <agent>
```

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `cc-relay-hub list` shows no agents | Run `cc-relay-hub bootstrap` and check cc-connect config files |
| Agent has no session | Send one normal chat message to that bot first |
| Reply does not return | Confirm the hook server is running and the cc-connect hook URL is configured |
| CDP agent is unreachable | Confirm the IDE was launched with a local debugging port |
| Fuzzy agent name is ambiguous | Use exact name or `send <query> --group <group>` |
