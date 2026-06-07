# cc-relay-hub

<p align="center">
  <img src="https://raw.githubusercontent.com/fengjunchengCode/cc-relay-hub/main/docs/assets/cc-relay-hub-readme-hero.png" alt="cc-relay-hub README hero showing the local relay hub mascot routing messages between a phone and local AI agent nodes" width="920">
</p>

<p align="center">
  <strong>Make your local AI coding agents talk to each other, so you do not have to copy messages between chat windows.</strong>
</p>

<p align="center">
  <a href="https://github.com/fengjunchengCode/cc-relay-hub/releases"><img alt="version" src="https://img.shields.io/github/v/release/fengjunchengCode/cc-relay-hub?label=version"></a>
  <a href="#-contributing--license--star-history--acknowledgments"><img alt="license" src="https://img.shields.io/badge/license-MIT-yellow"></a>
  <a href="https://github.com/chenhg5/cc-connect"><img alt="built with cc-connect" src="https://img.shields.io/badge/built%20with-cc--connect-blue"></a>
  <a href="./README_CN.md"><img alt="Chinese README" src="https://img.shields.io/badge/README-中文-red"></a>
</p>

<p align="center">
  English | <a href="./README_CN.md">中文</a>
</p>

**Use your phone to command local AI coding agents, then let those agents quietly hand work to each other.**

cc-connect already makes the first part work: you can talk to Claude Code, Codex, Gemini CLI, or other local agents from Feishu, WeChat, Telegram, Slack, and more. The pain starts when you have more than one agent running. Claude needs Codex to implement something. Hermes wants to dispatch work to several agents. An IDE agent needs to report back without filling a group chat with bot noise.

cc-relay-hub adds that private agent-to-agent handoff. Agents can discover peers, send tasks, wait for replies, and bring the answer back to the conversation where the work started.

<p align="center">
  <strong>Local first</strong> · <strong>Agent agnostic</strong> · <strong>Quiet handoffs</strong> · <strong>Reply loops that close</strong>
</p>

## ✨ Why cc-relay-hub?

- 📱 <strong>Command agents from your phone</strong> — Keep the cc-connect mobile workflow, then let local agents continue the work without you switching windows.
- 🤫 <strong>Keep chats quiet</strong> — Avoid group-bot explosions; talk to agents separately while relay messages move in the background.
- 🔁 <strong>Close the loop automatically</strong> — Send work from Claude to Codex and get the answer back where the request started.
- 🧭 **Let agents find the right teammate** — `cc-relay-hub list` shows live peers, while generated project instructions teach new agents how to use them.
- 🧩 **Bridge CLI agents and IDE agents** — Use cc-connect-backed agents for Claude Code and Codex, and CDP-backed agents for Electron IDEs such as Antigravity.
- ⏳ **Close replies without blocking** — plain `send` returns immediately while the hook server forwards marked replies back to the origin chat.

## 🛤️ Why I built this

I built cc-relay-hub because I was using cc-connect every day on my commute. From my phone, I could ask local agents to work through Feishu or WeChat, but multi-agent handoff was still manual: copy a task here, paste a result there, or put every bot in one noisy group. I wanted to keep the private one-on-one chat flow and add a controlled way for agents to pass work to each other.

<!-- TODO: Author can replace this with a more personal commute story. -->

## 🆚 cc-relay-hub and cc-connect multi-bot chat

| When you want to... | cc-connect multi-bot chat | cc-relay-hub |
|---|---|---|
| Use a local agent from your phone | Great fit | Keeps that workflow |
| Talk privately with one agent | Great fit | Keeps chats separate |
| Ask Claude to hand work to Codex | You copy and paste, or @ in a group | `send` delivers it directly and returns |
| Coordinate several agents | Group chat can get noisy | Hermes can dispatch quietly |
| Wait for another agent's answer | You watch the chat | The hook server forwards marked replies without blocking the caller |
| Bring an IDE agent into the team | Not the main path | CDP can connect Antigravity or Cursor |

## 🚀 Quick Start

cc-relay-hub is a local CLI + a tiny hook server. Both run on 127.0.0.1.

### Option A: Let an AI agent install it for you

Send this prompt to Claude Code, Codex, Cursor Agent, or any local coding agent:

```text
Follow https://raw.githubusercontent.com/fengjunchengCode/cc-relay-hub/refs/heads/main/INSTALL.md to install and configure cc-relay-hub.
```

The installer guide is written for agents. It checks prerequisites, reuses cc-connect's automated setup when possible, writes the local relay configuration, starts the hook server, and verifies the first route.

### Option B: Manual install

<details>
<summary>Show the manual path</summary>

```bash
git clone https://github.com/fengjunchengCode/cc-relay-hub.git ~/.cc-connect/cc-relay-hub

mkdir -p ~/bin
ln -sf ~/.cc-connect/cc-relay-hub/bin/cc-relay-hub ~/bin/cc-relay-hub
case ":$PATH:" in
  *":$HOME/bin:"*) ;;
  *) echo 'export PATH="$HOME/bin:$PATH"' >> ~/.zshrc; export PATH="$HOME/bin:$PATH" ;;
esac
hash -r

cc-relay-hub bootstrap
cc-relay-hub list
```

If this is your first cc-connect setup, start with [INSTALL.md](./INSTALL.md). For operational details, see [docs/operations.md](./docs/operations.md).

</details>

Most-used commands:

```bash
cc-relay-hub bootstrap
cc-relay-hub list
cc-relay-hub send <agent> "task"
Get-Content task.md -Raw | cc-relay-hub send <agent> --stdin
cc-relay-hub send <agent> "status update" --no-reply
cc-relay-hub relay <from-agent> <to-agent> "task"  # synchronous diagnostic handoff
cc-relay-hub watch
```

Private agent-to-agent messages are asynchronous by default: send the task, then let the target's marked reply be forwarded by the hook server. Do not open listeners or use `--wait` from an agent conversation unless a human explicitly asks for a synchronous diagnostic wait.

## 💬 Typical workflows

### 1. Claude sends a private task to Codex

You finish the plan in Claude Code, then relay the implementation to Codex:

```bash
cc-relay-hub send codex "Implement this plan and run the tests"
```

Codex works in its own session and the marked result comes back to Claude's current conversation through the hook server. Claude does not open a listener while Codex handles the patch.

<!-- TODO: Screenshot/GIF: Claude Code runs `cc-relay-hub relay ...`, Codex replies, result returns to Claude. Suggested path: docs/assets/demo-relay-claude-codex.gif -->

### 2. Hermes dispatches work to multiple agents

Hermes can discover the local agent network, pick targets by name or group, and send different tasks to different peers. One agent can implement, another can review, and a third can inspect docs or logs.

```bash
cc-relay-hub list --format json
cc-relay-hub send codex "Implement the parser change"
cc-relay-hub send claude "Review the patch for regressions"
```

The point is not a louder group chat. It is a quiet dispatcher that keeps each agent's work in the right lane.

<!-- TODO: Screenshot: Hermes lists grouped agents and dispatches tasks to multiple peers. Suggested path: docs/assets/screenshot-hermes-dispatch.png -->

### 3. Hermes controls Antigravity IDE and gets a reply

Some work belongs inside an IDE. With a CDP-backed agent, Hermes can send a task into Antigravity, let the IDE agent act, and collect the reply back through the same relay workflow.

```bash
cc-relay-hub cdp status antigravity-ide
cc-relay-hub send antigravity-ide "Open the project and inspect the failing workflow"
```

This brings Electron IDE agents into the same local team as Claude Code and Codex.

<!-- TODO: Screenshot/GIF: Hermes sends a task to Antigravity through CDP and receives the reply. Suggested path: docs/assets/demo-antigravity-cdp.gif -->

## 🧩 Support matrix

| Agent / tool | Route | Status | Best for |
|---|---|:---:|---|
| Claude Code | cc-connect | ✅ | Planning, review, long-running coding chats |
| Codex | cc-connect | ✅ | Implementation, tests, focused patches |
| Gemini CLI | cc-connect | ✅ | Research, alternative review, summarization |
| Cursor | cc-connect or CDP | ⚠️ | IDE workflows; backend maturity depends on local setup |
| Antigravity | CDP | ✅ | IDE automation through a local Electron window |
| Hermes | local orchestrator | ✅ | Discovering peers, dispatching work, collecting replies |
| Other Electron IDEs | CDP backend | ⚠️ | Possible when a backend can find the chat UI |
| Windows | local CLI | ⚠️ | Core code is portable, but the happy path is currently macOS/Linux-tested. |
| Cross-machine relay | custom network setup | ⚠️ | Default mode is same-machine; cross-machine use needs your own security boundary. |

## ❓ FAQ

<details>
<summary><strong>How is this related to cc-connect?</strong></summary>

cc-connect connects people to local agents through chat apps. cc-relay-hub builds on that workflow and lets those local agents pass work to each other privately.

</details>

<details>
<summary><strong>Do I have to install cc-connect?</strong></summary>

For Claude Code, Codex, Gemini CLI, and chat-backed agents, yes. CDP-backed IDE agents can be added separately, but the easiest setup starts with cc-connect.

</details>

<details>
<summary><strong>Is CDP safe?</strong></summary>

CDP can control an IDE window. Keep the debugging port on localhost, do not expose it to the network, and close it when you do not need IDE automation.

</details>

<details>
<summary><strong>Does it support Windows?</strong></summary>

The project is designed around portable Python and local processes, but the current happy path is macOS/Linux. Windows support should be treated as community-tested until more reports land.

</details>

<details>
<summary><strong>Can it run across machines?</strong></summary>

The default design is same-machine. Cross-machine routing is possible only if you provide networking, authentication, and port security yourself.

</details>

## 📖 Advanced docs

- [Architecture](./docs/architecture.md) — internals, diagrams, routing lifecycle
- [Providers](./docs/providers.md) — cc-connect and CDP provider setup
- [CLI Reference](./docs/cli.md) — full command list
- [Operations](./docs/operations.md) — hook server, session setup, troubleshooting
- [AI-agent install guide](./INSTALL.md) — installation steps an agent can execute

## 🤝 Contributing / License / Star History / Acknowledgments

Contributions are welcome: new CDP backends, provider adapters, real screenshots, workflow examples, and docs fixes are all useful.

MIT licensed.

## ⭐ Star History

<a href="https://www.star-history.com/#fengjunchengCode/cc-relay-hub&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=fengjunchengCode/cc-relay-hub&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=fengjunchengCode/cc-relay-hub&type=Date" />
   <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=fengjunchengCode/cc-relay-hub&type=Date" />
 </picture>
</a>

## 🙏 Acknowledgments

Built on top of [cc-connect](https://github.com/chenhg5/cc-connect), and inspired by real multi-agent coding workflows with Claude Code, Codex, Hermes, and Antigravity.

<!-- TODO: Add real screenshots/GIFs before the next public launch. -->
