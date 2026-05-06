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

## Commands

```bash
cc-relay-hub list --format json
cc-relay-hub info <agent>
cc-relay-hub send <agent> "<message>"
cc-relay-hub send <agent> "<message>" --wait --timeout 300
```

## Recommended flow

1. Run `cc-relay-hub list --format json` and choose a target from current live output.
2. Run `cc-relay-hub info <agent>` to confirm webhook/session health.
3. Send a concrete task with `cc-relay-hub send`.
4. Use `--wait` only when you need serialized request/reply flow on that target session.

## Notes

- Reply monitoring depends on the local hook server and `message.sent` hooks being configured in cc-connect.
- If `info` or `list` shows `session_key` as empty or `none`, send one normal chat message to that bot in its own chat window, then rerun discovery.
