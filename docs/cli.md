# CLI Reference

The wrapper command is `cc-relay-hub`. It invokes `hub.py` with Python 3.9+.

## Common Commands

| Command | Description |
| --- | --- |
| `cc-relay-hub bootstrap` | Scan configs, write registry/bindings, verify connectivity |
| `cc-relay-hub list` | Discover configured local agents |
| `cc-relay-hub send <agent> "<msg>"` | Send an asynchronous private request; marked replies are forwarded to the origin session |
| `cc-relay-hub send <agent> --stdin` | Send a multiline private request from stdin |
| `cc-relay-hub send <agent> "<msg>" --no-reply` | Send a notice that must not trigger a reply |
| `cc-relay-hub watch` | Diagnostic one-shot long-poll for new hook events |

## Full Command Table

| Command | Description |
| --- | --- |
| `cc-relay-hub bootstrap` | Scan configs, write registry/bindings, verify connectivity |
| `cc-relay-hub bootstrap context --print` | Print generated agent context blocks |
| `cc-relay-hub bootstrap context --check` | Check global and workdir context blocks |
| `cc-relay-hub bootstrap context --write` | Install/update global memory and known workdir context blocks |
| `cc-relay-hub bootstrap context --write --scope cwd` | Write only the current directory context files |
| `cc-relay-hub list` | Discover configured local agents |
| `cc-relay-hub list --format json` | Return machine-readable peer data for agent skills |
| `cc-relay-hub list --group <group>` | List only agents in a group |
| `cc-relay-hub info <agent>` | Show provider, session, and health details |
| `cc-relay-hub send <agent> "<msg>"` | Send a message to a peer agent |
| `cc-relay-hub send <agent> "<msg>" --wait` | Send and wait for the matched reply; use only for explicit synchronous diagnostics |
| `cc-relay-hub send <agent> "<msg>" --no-reply` | Send without a reply marker or pending session lock |
| `cc-relay-hub send <agent> "<msg>" --group <group>` | Resolve the target within a group |
| `cc-relay-hub send <agent> --stdin` | Read the full message from stdin |
| `cc-relay-hub send <agent> --message-file task.md` | Read the full message from a UTF-8 file |
| `cc-relay-hub groups` | List groups |
| `cc-relay-hub groups show <name>` | Show group members |
| `cc-relay-hub groups create <name>` | Create a group |
| `cc-relay-hub groups delete <name>` | Delete a group |
| `cc-relay-hub groups join <group> <agent>` | Add an agent to a group |
| `cc-relay-hub groups leave <group> <agent>` | Remove an agent from a group |
| `cc-relay-hub relay <from> <to> "<msg>"` | Send from one agent to another and wait |
| `cc-relay-hub relay <from> <to> --stdin` | Relay a multiline message from stdin |
| `cc-relay-hub watch` | One-shot long-poll for hook events |
| `cc-relay-hub watch --loop` | Continuous event stream for a human diagnostic terminal; do not use from an agent conversation |
| `cc-relay-hub cdp status <agent>` | Check CDP-backed agent health |
| `cc-relay-hub cdp screenshot <agent>` | Capture a screenshot through CDP |
| `cc-relay-hub cdp models <agent>` | List models if the backend supports it |
| `cc-relay-hub cdp switch <agent> <model>` | Switch model if the backend supports it |
| `cc-relay-hub cdp probe <agent>` | Print a DOM probe for diagnostics |
| `cc-relay-hub cdp heal <agent>` | Run selector self-healing for chat input |

CDP agents are normal `send` targets:

```bash
cc-relay-hub info antigravity-ide
cc-relay-hub cdp status antigravity-ide
cc-relay-hub send antigravity-ide "task"
```

For private agent-to-agent messages, decide before sending whether a reply is needed. If a reply is needed, use plain `send` and return; the target's `[cc-relay reply_to=...]` answer is forwarded by the hook server. If no reply is needed, use `--no-reply`. Do not use `watch`, `watch --loop`, raw long-poll, shell polling, or `send --wait` to wait from an agent conversation unless a human explicitly asks for a synchronous diagnostic wait.

For multiline or long messages, prefer stdin or a UTF-8 file. This is required on Windows when the message comes from a PowerShell here-string or variable; passing that variable through the `.cmd` wrapper as positional `"<msg>"` can lose content before Python receives it.

```powershell
Get-Content .\task.md -Raw | cc-relay-hub send my-project --stdin
cc-relay-hub send my-project --message-file .\task.md
```

For CDP agents, an empty `Session` or `Last Seen: never` is expected; use `cdp status/probe/heal/screenshot` for IDE-side diagnostics.

## Agent Resolution

`send`, `info`, and `relay` support exact and fuzzy agent names:

1. Exact name match wins.
2. Fuzzy match checks name substring and `type`.
3. `--group` narrows candidates for `send`.
4. Sender's group is preferred when available.
5. Ambiguous matches ask for an exact name.

This lets commands such as `send codex "task"` choose the Codex-like agent in the same group when that is unambiguous.
Exact names do not bypass group isolation: `send` and `relay` reject cross-group targets, including ungrouped-to-named-group sends.
