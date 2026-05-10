# cc-relay-hub Agent Context

You are connected to **cc-relay-hub**, a local multi-agent message router.
Use it to discover peers, check health, and delegate work to other agents.

## Discovery

```bash
cc-relay-hub list --format json     # discover all agents
cc-relay-hub info <agent>           # check agent health
cc-relay-hub groups                 # list groups and members
```

## Sending Messages

```bash
cc-relay-hub send <agent> "task description" --wait --timeout 120
```

- Delivers via webhook HTTP POST (not platform API).
- `--wait` blocks until the target replies or timeout.
- Always check `info <agent>` before sending.

## Relay (Agent-to-Agent)

```bash
cc-relay-hub relay <from> <to> "task" --timeout 120
```

- Sends from one agent to another within the same group.
- Always waits for reply.

## Relay Protocol

When you receive a message containing `[cc-relay request_id=...]`:
1. Read the task after the marker.
2. Start your final response with: `[cc-relay reply_to=<same_id>]`
3. Put your answer after that marker line.
4. Do not use this marker for any other purpose.
5. Never answer `NO_REPLY` or an empty response to a cc-relay-hub request.
6. If the task says "only reply X", still put `X` after the required reply marker.

## Groups

Agents are organized into groups. Same-group agents can relay to each other.
Check your group: `cc-relay-hub list --format json`

## Rules

- Never hardcode agent names. Discover with `cc-relay-hub list`.
- Never use shell polling loops (`tail -f`, `while true`, `sleep`).
- Use `cc-relay-hub send --wait` for request/reply.
- Check agent health before sending work.

## Agent Name Resolution

When you use `send`, `info`, or `relay`, the agent name is resolved as:
1. **Exact match** — `send codex-bot` finds `codex-bot` directly.
2. **Fuzzy match** — `send codex` matches agents with "codex" in name or type.
3. **Same-group preference** — among fuzzy matches, the agent in your group is preferred. You are identified by the `CC_PROJECT` environment variable.

If multiple agents match and you're unsure, use `cc-relay-hub list` to see exact names.
