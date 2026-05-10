---
name: cc-relay
description: Use when the agent needs to discover peer agents on the current machine, delegate work through cc-relay-hub, or inspect relay status before messaging another agent.
---

# cc-relay

You are operating in a multi-agent network on the current machine. Use `cc-relay-hub` to discover peers, check routing state, and send work to another agent.

## Rules

- Do not hardcode agent names or member lists.
- Discover current peers with `cc-relay-hub list --format json`.
- Check a target before sending work with `cc-relay-hub info <agent>`.
- Use one target session at a time. Phase 1a enforces a single pending outbound write per target session.
- When the user says they added a new cc-connect instance or changed work directories with cc-connect `/dir`, run `cc-relay-hub bootstrap` to re-scan, verify connectivity, and refresh global/workdir agent context blocks.
- When you receive `[cc-relay request_id=...]`, always reply with `[cc-relay reply_to=...]`; never answer `NO_REPLY` or omit the marker because the task says "only reply X".

## Routing Contract

`cc-connect relay` and `cc-relay-hub` are different systems:

- Use `cc-relay-hub` for direct/private agent-to-agent delegation: "send to codex", "ask Claude", "let another agent continue", or "relay to <agent>".
- Use `cc-connect relay` only for cc-connect's group-chat relay feature after a chat has been bound with `/bind`.
- Do not use `cc-connect relay send` as the default implementation for "send a message to codex".
- If the user asks to contact another coding agent by name or type, default to `cc-relay-hub`.

## Agent Resolution

`send`, `info`, and `relay` commands resolve agent names with same-group preference:

1. **Exact name match** — always wins (`send codex-bot` → finds `codex-bot`).
2. **Fuzzy match** — if no exact match, matches by type or name substring (`send codex` → finds agents with "codex" in name or type=codex).
3. **Same-group preference** — among fuzzy matches, prefers the agent sharing a group with the sender (detected via `CC_PROJECT` env var). The sender itself is excluded from candidates.
4. **Disambiguation** — if multiple same-group matches remain, the command errors with a list. Use exact name.

This means: if you have `codex-alpha` (group A) and `codex-beta` (group B), and you're in group A, `send codex "msg"` automatically picks `codex-alpha`.

## Commands

```bash
# Discovery
cc-relay-hub list [--group <name>] [--format json|table]
cc-relay-hub info <agent>

# Send (via webhook, NOT Feishu API)
cc-relay-hub send <agent> "<message>"
cc-relay-hub send <agent> "<message>" --wait --timeout 300
cc-relay-hub send <agent> "<message>" --group <group>

# Groups
cc-relay-hub groups                          # list all groups
cc-relay-hub groups show <name>              # show group members + status
cc-relay-hub groups create <name> [--desc "..."]
cc-relay-hub groups join <group> <agent>
cc-relay-hub groups leave <group> <agent>

# Relay (agent-to-agent, always waits)
cc-relay-hub relay <from-agent> <to-agent> "<message>"

# Events
cc-relay-hub watch                        # one-shot: block until an event arrives
cc-relay-hub watch --loop --format text   # continuous: stream events as they arrive

# CDP (Electron IDE)
cc-relay-hub cdp status <agent>
cc-relay-hub cdp models <agent>
cc-relay-hub cdp screenshot <agent>
```

## Recommended flow

1. Run `cc-relay-hub list --format json` and choose a target from current live output.
2. Run `cc-relay-hub info <agent>` to confirm webhook/session health.
3. Send a concrete task with `cc-relay-hub send`.
4. Use `--wait` only when you need serialized request/reply flow on that target session.
5. To check for incoming events, use `cc-relay-hub watch` (one-shot) or `cc-relay-hub watch --loop`.

## CRITICAL: How delivery works

`cc-relay-hub send` delivers via **HTTP POST to cc-connect webhook** (not Feishu API).
- `send codex-bot "msg"` → POST to `http://127.0.0.1:9112/hook`
- `send my-project "msg"` → POST to `http://127.0.0.1:9110/hook`
- No echo filter — the message is processed by cc-connect and forwarded to the agent.
- Do NOT use Feishu API directly to send messages to bots (echo filter drops bot's own messages).

## CRITICAL: Never use shell polling loops

**Do NOT use `tail -f`, `while true`, `sleep` loops, or any shell-based file
monitoring to watch for hook events.** These block the agent's conversation and
prevent it from receiving new user messages.

Instead use:
- `cc-relay-hub send <agent> "<msg>" --wait` for request/reply (blocks in Python, not shell)
- `cc-relay-hub watch` for one-shot event check (single HTTP long-poll)
- `cc-relay-hub watch --loop` for continuous streaming (single Python process, no shell loop)
- `curl http://127.0.0.1:9120/events/longpoll?since=<ISO>&timeout=30` for raw HTTP long-poll

## Notes

- Reply monitoring depends on the local hook server and `message.sent` hooks being configured in cc-connect.
- If `info` or `list` shows `session_key` as empty or `none`, send one normal chat message to that bot in its own chat window, then rerun discovery.
- The hook server's long-poll endpoint (`GET /events/longpoll`) holds the HTTP connection open until events arrive. This is the correct way to wait for events without polling.
