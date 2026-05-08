# CLI Reference

The wrapper command is `cc-relay-hub`. It invokes `hub.py` with Python 3.9+.

## Common Commands

| Command | Description |
| --- | --- |
| `cc-relay-hub bootstrap` | Scan configs, write registry/bindings, verify connectivity |
| `cc-relay-hub list` | Discover configured local agents |
| `cc-relay-hub send <agent> "<msg>" --wait` | Send and wait for the matched reply |
| `cc-relay-hub relay <from> <to> "<msg>"` | Send a group-scoped agent-to-agent request and wait |
| `cc-relay-hub watch` | One-shot long-poll for new hook events |

## Full Command Table

| Command | Description |
| --- | --- |
| `cc-relay-hub bootstrap` | Scan configs, write registry/bindings, verify connectivity |
| `cc-relay-hub bootstrap context --print` | Print generated agent context files |
| `cc-relay-hub bootstrap context --check` | Check whether generated context files are up to date |
| `cc-relay-hub bootstrap context --write` | Generate AGENTS.md and agent-native context files |
| `cc-relay-hub list` | Discover configured local agents |
| `cc-relay-hub list --format json` | Return machine-readable peer data for agent skills |
| `cc-relay-hub list --group <group>` | List only agents in a group |
| `cc-relay-hub info <agent>` | Show provider, session, and health details |
| `cc-relay-hub send <agent> "<msg>"` | Send a message to a peer agent |
| `cc-relay-hub send <agent> "<msg>" --wait` | Send and wait for the matched reply |
| `cc-relay-hub send <agent> "<msg>" --group <group>` | Resolve the target within a group |
| `cc-relay-hub groups` | List groups |
| `cc-relay-hub groups show <name>` | Show group members |
| `cc-relay-hub groups create <name>` | Create a group |
| `cc-relay-hub groups delete <name>` | Delete a group |
| `cc-relay-hub groups join <group> <agent>` | Add an agent to a group |
| `cc-relay-hub groups leave <group> <agent>` | Remove an agent from a group |
| `cc-relay-hub relay <from> <to> "<msg>"` | Send from one agent to another and wait |
| `cc-relay-hub watch` | One-shot long-poll for hook events |
| `cc-relay-hub watch --loop` | Continuous event stream for a human terminal or tmux pane |
| `cc-relay-hub cdp status <agent>` | Check CDP-backed agent health |
| `cc-relay-hub cdp screenshot <agent>` | Capture a screenshot through CDP |
| `cc-relay-hub cdp models <agent>` | List models if the backend supports it |
| `cc-relay-hub cdp switch <agent> <model>` | Switch model if the backend supports it |
| `cc-relay-hub cdp probe <agent>` | Print a DOM probe for diagnostics |
| `cc-relay-hub cdp heal <agent>` | Run selector self-healing for chat input |

## Agent Resolution

`send`, `info`, and `relay` support exact and fuzzy agent names:

1. Exact name match wins.
2. Fuzzy match checks name substring and `type`.
3. `--group` narrows candidates for `send`.
4. Sender's group is preferred when available.
5. Ambiguous matches ask for an exact name.

This lets commands such as `send codex "task"` choose the Codex-like agent in the same group when that is unambiguous.
