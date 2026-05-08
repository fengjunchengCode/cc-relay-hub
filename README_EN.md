# cc-relay-hub

English documentation is also maintained in [README.md](README.md). This file exists for tools and agents that explicitly look for `README_EN.md`.

cc-relay-hub is a local coordination layer for AI agents, cc-connect bots, and CDP-controlled Electron IDEs. It adds discovery, routing, request/reply tracking, session locks, group-aware relay, and context bootstrapping on top of local transports.

## Why It Exists

[cc-connect](https://github.com/chenhg5/cc-connect) connects local coding agents to chat platforms. cc-relay-hub focuses on the next layer: agent-driven orchestration.

| Capability | cc-connect multi-agent flow | cc-relay-hub |
| --- | --- | --- |
| Main job | Connect humans to local agents through chat platforms | Let local agents discover and delegate to each other |
| Entry point | Chat app commands and group messages | CLI/skill calls from inside an agent |
| Target selection | Human chooses or mentions a bot | Agent discovers peers with `cc-relay-hub list` |
| Request tracking | Chat history oriented | SQLite state, request IDs, session locks, reply attribution |
| Reply path | Reply appears in the chat platform conversation | Reply returns to the originating agent session |
| IDE reach | Agents with cc-connect transport | cc-connect agents plus Electron IDEs exposed over local CDP |

## Providers

| Provider | Transport | Reply detection | Best fit |
| --- | --- | --- | --- |
| `cc_connect` | Local HTTP webhook | `message.sent` hook server | Claude Code, Codex, or any agent bridged by cc-connect |
| `cdp` | Chrome DevTools Protocol WebSocket | DOM transcript polling with relay markers | Electron IDEs such as Antigravity, Cursor, or other local IDE UIs |

CDP agents use the same relay protocol as cc-connect agents. The provider types the prompt into the IDE, waits for the answer in the DOM transcript, and extracts the `[cc-relay reply_to=...]` marker. CDP replies do not require the hook server.

Keep CDP ports bound to localhost. A CDP port gives automation access to the IDE window.

## Agent Groups

Groups provide weak isolation for agent-to-agent communication. Agents in the same group can relay to each other; for backward compatibility, agents with no group assignment can still communicate.

```bash
cc-relay-hub groups create core --description "Main project agents"
cc-relay-hub groups join core claudecode
cc-relay-hub groups join core codex-bot
cc-relay-hub groups show core
```

## Relay Command

Use `relay` when one agent should send a task as itself to another agent and wait for a reply:

```bash
cc-relay-hub relay claudecode codex-bot "Review the failing test and suggest a fix" --timeout 120
```

`relay` always waits. If the origin agent is a cc-connect provider with a session key, the reply is also forwarded back into the origin session.

## Bootstrap Context

`bootstrap context` generates agent-native context files from the current registry:

```bash
cc-relay-hub bootstrap context --print
cc-relay-hub bootstrap context --check
cc-relay-hub bootstrap context --write
```

Generated files include `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, and `.cursorrules`. When multiple agents are registered, per-agent files are written under `.cc-relay-hub/<agent>/` so each agent receives the right identity, peer list, and relay protocol.

## Quick Start

The recommended path is to let a local coding agent perform the setup:

```text
Follow https://raw.githubusercontent.com/fengjunchengCode/cc-relay-hub/refs/heads/main/INSTALL.md to install and configure cc-relay-hub.
```

The install guide is written for agents and prefers automated cc-connect setup paths such as `cc-connect web`, `cc-connect feishu setup`, and `cc-connect weixin setup`. Users should not need to edit TOML manually for the common path.

Manual fallback:

```bash
git clone https://github.com/fengjunchengCode/cc-relay-hub.git ~/.cc-connect/cc-relay-hub
mkdir -p ~/bin
ln -sf ~/.cc-connect/cc-relay-hub/bin/cc-relay-hub ~/bin/cc-relay-hub
```

## Core Commands

| Command | Description |
| --- | --- |
| `cc-relay-hub bootstrap` | Scan configs, write registry, verify connectivity |
| `cc-relay-hub bootstrap context --write` | Generate AGENTS.md and agent-native context files |
| `cc-relay-hub list --format json` | Return machine-readable peer data for agent skills |
| `cc-relay-hub info <agent>` | Show provider and agent health |
| `cc-relay-hub send <agent> "<msg>" --wait` | Send and wait for the matched reply |
| `cc-relay-hub groups` | List and manage agent groups |
| `cc-relay-hub relay <from> <to> "<msg>"` | Send a group-scoped agent-to-agent request and wait |
| `cc-relay-hub cdp status <agent>` | Check a CDP-backed Electron IDE agent |

See [README.md](README.md) and [INSTALL.md](INSTALL.md) for the full guide.
