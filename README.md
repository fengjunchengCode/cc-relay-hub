# cc-relay-hub

<p align="center">A local dispatch layer for AI agents already connected by cc-connect.</p>

<p align="center">
  <a href="https://github.com/fengjunchengCode/cc-relay-hub/blob/main/README_zh.md">中文</a> · English
</p>

---

## What it is

[cc-connect](https://github.com/chenhg5/cc-connect) brings local coding agents into your chat apps. **cc-relay-hub** adds the missing layer between those agents: discovery, routing, request/reply tracking, and reply forwarding.

Use it when one agent should hand work to another without asking you to copy messages between chat windows.

Example:

```text
Claude Code: plan the migration
cc-relay-hub: send the implementation brief to Codex
Codex: build and report back
cc-relay-hub: forward the reply to Claude Code's original chat session
```

This is not a replacement for cc-connect. It is a small local coordination layer that uses cc-connect's webhook and hook system as the transport.

> Single host only. cc-relay-hub reads local cc-connect config files, talks to local webhook endpoints, and runs its hook server on `127.0.0.1`.

## Why not just use cc-connect multi-agent chat?

cc-connect already supports multiple agents and multi-bot relay in chat groups. That is great for human-driven collaboration: you mention or message the bot you want, and the conversation happens in the chat app.

cc-relay-hub is different. It is built for **agent-driven orchestration**.

| Capability | cc-connect multi-agent flow | cc-relay-hub |
| --- | --- | --- |
| Main job | Connect humans to local agents through chat platforms | Let local agents discover and delegate to each other |
| Entry point | Chat app commands and group messages | CLI/skill calls from inside an agent |
| Target selection | Human chooses or mentions a bot | Agent discovers peers with `cc-relay-hub list` |
| Request tracking | Chat history oriented | SQLite state, request IDs, session locks, reply attribution |
| Reply path | Reply appears in the chat platform conversation | Reply is forwarded back to the originating agent session |
| Waiting model | Human watches the chat | `send --wait` and long-poll `watch`, safe for agent tools |

The practical benefit is that an agent can run a controlled handoff:

- discover current peers instead of relying on hardcoded bot names
- check webhook and session health before sending work
- send a bounded task to one target session at a time
- wait for a reply without shell polling loops
- receive the reply in the original chat context

That makes it better suited for planner-builder, reviewer-implementer, or specialist-agent workflows where the initiating agent needs a concrete answer back before continuing.

## How it works

```text
Chat App
   |
   v
cc-connect project A  -- cc-relay-hub send -->  cc-connect project B
   ^                                                |
   |                                                v
   +------- hook server receives message.sent <-----+
            and forwards reply to project A
```

1. Agent A calls `cc-relay-hub send <agent> "<task>"`
2. cc-relay-hub discovers Agent B from local cc-connect config and session files
3. The task is delivered through Agent B's local cc-connect webhook
4. Agent B replies; cc-connect emits a `message.sent` hook
5. cc-relay-hub matches the reply to the original request and sends it back to Agent A's session

## Quick Start

### Install and configure via AI agent

The recommended path is to let Claude Code, Codex, Gemini CLI, Cursor Agent, or another local coding agent run the setup for you.

Send this prompt to your agent:

```text
Follow https://raw.githubusercontent.com/fengjunchengCode/cc-relay-hub/refs/heads/main/INSTALL.md to install and configure cc-relay-hub.
```

The install guide is written for agents: it contains checks, exact commands, validation steps, and troubleshooting branches. In normal use, the user should not need to touch TOML manually.

### Manual fallback

Use this only if you want to inspect or run the setup yourself.

```bash
git clone https://github.com/fengjunchengCode/cc-relay-hub.git ~/.cc-connect/cc-relay-hub

mkdir -p ~/bin
ln -sf ~/.cc-connect/cc-relay-hub/bin/cc-relay-hub ~/bin/cc-relay-hub
case ":$PATH:" in
  *":$HOME/bin:"*) ;;
  *) echo 'export PATH="$HOME/bin:$PATH"' >> ~/.zshrc; export PATH="$HOME/bin:$PATH" ;;
esac
hash -r
```

Every participating cc-connect config needs:

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

Use a unique webhook port for each running cc-connect process, then restart cc-connect and start the relay hook server:

```bash
node ~/.cc-connect/cc-relay-hub/hook-server.mjs
```

## Verify

```bash
cc-relay-hub list
cc-relay-hub info <agent>
cc-relay-hub send <agent> "Ping from another agent" --wait --timeout 120
```

`Session` should not be `none`. If it is, open that bot's own chat window, send one normal message, then run `cc-relay-hub list` again.

## CLI

| Command | Description |
| --- | --- |
| `cc-relay-hub bootstrap` | Scan configs, write registry, verify connectivity |
| `cc-relay-hub list` | Discover configured local agents |
| `cc-relay-hub list --format json` | Return machine-readable peer data for agent skills |
| `cc-relay-hub info <agent>` | Show webhook, session, and health details |
| `cc-relay-hub send <agent> "<msg>"` | Send a message to a peer agent |
| `cc-relay-hub send <agent> "<msg>" --wait` | Send and wait for the matched reply |
| `cc-relay-hub watch` | One-shot long-poll for new hook events |
| `cc-relay-hub watch --loop` | Continuous event stream for a human terminal or tmux pane |

## Agent skill

The repo ships a generic relay skill:

```text
skills/cc-relay.md
```

For Claude Code:

```bash
mkdir -p ~/.claude/skills
cp ~/.cc-connect/cc-relay-hub/skills/cc-relay.md ~/.claude/skills/
```

For other agents, load the same file into their project instruction or skill system. The skill discovers peers at runtime with `cc-relay-hub list --format json`; it does not hardcode names.

## Architecture

```text
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   CLI/Skill  │────►│  Relay Core  │────►│   Provider   │
│              │◄────│  state.db    │◄────│ cc-connect   │
└──────────────┘     └──────────────┘     └──────────────┘
                           │
                     ┌─────┴─────┐
                     │ Hook Server│ :9120
                     └───────────┘
```

- **Discovery** reads local `~/.cc-connect/config*.toml` files and session metadata
- **Provider** sends through cc-connect local webhooks
- **State** stores request IDs, session locks, delivery status, replies, and notification status
- **Hook server** receives `message.sent` events and forwards matched replies to the origin session

## Operational notes

- After adding a new cc-connect instance, run `cc-relay-hub bootstrap` to re-scan and verify connectivity
- Enable `[webhook]` for both the origin project and the target project
- Add the `message.sent` hook to projects that should return replies through the relay
- Bootstrap each bot once by sending a normal message in its own chat window
- Do not use `tail -f`, `while true`, or shell polling loops for events; use `send --wait` or `watch`
- The hook server listens on `127.0.0.1:9120` by default

## Full installation guide

See [INSTALL.md](INSTALL.md). It is intentionally written so an AI coding agent can execute the setup and verify each step.

## License

MIT
