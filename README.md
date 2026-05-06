# cc-relay-hub

<p align="center">Route messages between any local AI agents. From any chat app.</p>

<p align="center">
  <a href="https://github.com/fengjunchengCode/cc-relay-hub/blob/main/README_zh.md">中文</a> · English
</p>

---

## What is cc-relay-hub?

**cc-relay-hub** is a lightweight message relay built on top of [cc-connect](https://github.com/chenhg5/cc-connect). It lets any AI agent (Claude Code, Codex, Gemini CLI, etc.) discover and send messages to any other agent through a unified CLI.

**Use case**: delegate tasks from one agent to another. For example, have Claude Code plan an architecture, then send the spec to Codex for implementation — all from your chat window.

> **Single host only.** cc-relay-hub runs on one machine. It reads local cc-connect config files, uses local webhook endpoints, and the hook server binds to `127.0.0.1`. It is not a multi-machine message bus.

## How it works

```
Chat App ──► cc-connect (Agent A) ──► cc-relay-hub send ──► cc-connect (Agent B)
                  ▲                                              │
                  │         hook: message.sent                   │
                  └──────────────────────────────────────────────┘
```

1. Agent A sends a message to Agent B via webhook
2. Agent B replies; cc-connect fires a `message.sent` hook
3. The hook server receives the event and pushes the reply back through Agent A's own cc-connect instance

## Quick Start

### Prerequisites

- [cc-connect](https://github.com/chenhg5/cc-connect) installed and configured
- Python 3.9+
- Node.js (for the hook server)
- At least two cc-connect projects configured

### Install

```bash
# Clone
git clone https://github.com/fengjunchengCode/cc-relay-hub.git ~/.cc-connect/cc-relay-hub

# Add CLI to PATH (bash users: replace .zshrc with .bashrc)
mkdir -p ~/bin
ln -sf ~/.cc-connect/cc-relay-hub/bin/cc-relay-hub ~/bin/cc-relay-hub
echo 'export PATH="$HOME/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

### Configure

**1. Enable webhook on ALL participating projects** (caller and target):

```toml
# In each project's cc-connect config
[webhook]
enabled = true
port = 9110    # use a unique port per project
path = "/hook"
```

**2. Add the reply hook** to each target project's config:

```toml
[[hooks]]
event = "message.sent"
type = "http"
url = "http://127.0.0.1:9120/cc-connect/hooks/reply"
async = false
timeout = 2
```

**3. Restart cc-connect** after config changes.

**4. Bootstrap sessions**: open each bot's chat window and send one normal message. This creates the initial session that `cc-relay-hub list` needs to discover.

**5. Start the hook server**:

```bash
node ~/.cc-connect/cc-relay-hub/hook-server.mjs
```

For auto-start on macOS, see [INSTALL.md](INSTALL.md).

### Verify

```bash
# Discover agents (Session should not be "none")
cc-relay-hub list

# Send a message
cc-relay-hub send codex-bot "Hello from Claude Code"

# Send and wait for reply
cc-relay-hub send codex-bot "Review this code" --wait --timeout 120
```

## CLI Reference

| Command | Description |
|---------|-------------|
| `cc-relay-hub list` | Discover all configured agents |
| `cc-relay-hub info <agent>` | Show agent details, webhook health, session status |
| `cc-relay-hub send <agent> "<msg>"` | Send a message to an agent |
| `cc-relay-hub send <agent> "<msg>" --wait` | Send and block until reply arrives |
| `cc-relay-hub watch` | One-shot long-poll for new hook events (safe for agents) |
| `cc-relay-hub watch --loop` | Continuous event streaming (best for human terminals / tmux) |

## Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   CLI/Skill  │────►│  Relay Core  │────►│   Provider   │
│              │◄────│  (envelope,  │◄────│  (cc-connect │
│              │     │   state.db)  │     │   webhook)   │
└──────────────┘     └──────────────┘     └──────────────┘
                           │
                     ┌─────┴─────┐
                     │ Hook Server│ :9120
                     │ (Node.js)  │
                     └───────────┘
```

- **Registry/Discovery**: reads cc-connect config files to find agents
- **Transport**: delivers messages via cc-connect webhook endpoints
- **State**: SQLite store tracks message lifecycle and reply attribution
- **Hook Server**: receives `message.sent` events, pushes replies back through the origin project's own cc-connect path

## Important Notes

- **Every participating project** must have `[webhook]` enabled — including the origin (caller) project
- **Bootstrap each bot** by sending one normal message in its chat window before using `cc-relay-hub`
- **Never use shell polling loops** (`tail -f`, `while true`) to monitor events — use `cc-relay-hub watch` (one-shot, safe for agents) or `cc-relay-hub send --wait` (request/reply)
- The hook server listens on `127.0.0.1:9120` by default

## Detailed Installation

See [INSTALL.md](INSTALL.md) for a step-by-step guide (designed for AI agents to follow automatically).

## License

MIT
