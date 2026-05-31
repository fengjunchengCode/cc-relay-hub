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
Get-Content task.md -Raw | cc-relay-hub send <agent> --stdin --wait --timeout 120
```

- For `cc_connect` agents, delivers via local webhook HTTP POST.
- For `cdp` agents such as Antigravity, delivers through the local Chrome DevTools Protocol session into the IDE agent chat.
- `--wait` blocks until the target replies or timeout.
- Always check `info <agent>` before sending.
- If `info` shows `Provider: cdp`, still use `cc-relay-hub send <agent> "task" --wait`; do not switch to `cc-connect relay`.
- For multiline or long tasks, use `--stdin` or `--message-file`; on Windows, do not pass a PowerShell multiline variable as positional `"task"`.

## CDP IDE Agents

CDP-backed agents are IDE windows controlled through localhost CDP. Antigravity commonly appears as `antigravity-ide`.

```bash
cc-relay-hub info antigravity-ide
cc-relay-hub cdp status antigravity-ide
cc-relay-hub send antigravity-ide "task description" --wait --timeout 120
```

Use diagnostics only when needed:

```bash
cc-relay-hub cdp probe antigravity-ide
cc-relay-hub cdp heal antigravity-ide
cc-relay-hub cdp models antigravity-ide
cc-relay-hub cdp screenshot antigravity-ide --path /tmp/antigravity.png
```

- `Last Seen: never` is normal for CDP agents because replies are read from the IDE DOM, not from the hook server.
- If `send --wait` times out, run `cdp status`, `cdp probe`, and `cdp screenshot` before retrying.
- Keep CDP ports bound to `127.0.0.1`; never expose the debugging port to a network.

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
- Do not send messages across groups. Treat ungrouped agents as separate from named groups; exact agent names do not bypass group isolation.
- Never use shell polling loops (`tail -f`, `while true`, `sleep`).
- Use `cc-relay-hub send --wait` for request/reply.
- Check agent health before sending work.

## Agent Name Resolution

When you use `send`, `info`, or `relay`, the agent name is resolved as:
1. **Exact match** — `send codex-bot` finds `codex-bot` directly.
2. **Fuzzy match** — `send codex` matches agents with "codex" in name or type.
3. **Same-group preference** — among fuzzy matches, the agent in your group is preferred. You are identified by the `CC_PROJECT` environment variable.

If multiple agents match and you're unsure, use `cc-relay-hub list` to see exact names.
