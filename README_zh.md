# cc-relay-hub

<p align="center">给已经接入 cc-connect 的本地 AI Agent 加一层调度中枢。</p>

<p align="center">
  中文 · <a href="https://github.com/fengjunchengCode/cc-relay-hub/blob/main/README.md">English</a>
</p>

---

## 它是什么

[cc-connect](https://github.com/chenhg5/cc-connect) 把本地 Coding Agent 接进飞书、Telegram、Slack、Discord、微信等聊天入口。**cc-relay-hub** 解决的是下一层问题：这些 Agent 之间如何互相发现、互相委派、等待回复，并把结果推回发起方所在的原始会话。

适合这样的场景：

```text
Claude Code：负责拆方案
cc-relay-hub：把实现任务发给 Codex
Codex：完成实现并回复
cc-relay-hub：把回复推回 Claude Code 的原始会话
```

它不是 cc-connect 的替代品，而是基于 cc-connect webhook / hook 能力构建的一层本地 Agent 调度层。

> 仅限单机使用。cc-relay-hub 读取本机 cc-connect 配置和 session 文件，访问本地 webhook，Hook Server 默认绑定 `127.0.0.1:9120`。

## 和 cc-connect 的多 Agent 编排有什么不同

cc-connect 已经支持多 Agent、多项目、多 Bot relay。它更适合“人通过聊天应用控制多个 Agent”：你在群里提问、点名、切换命令，Agent 在聊天上下文里协作。

cc-relay-hub 的重心不是人手动点名，而是 **Agent 主动编排 Agent**。

| 能力 | cc-connect 多 Agent 流程 | cc-relay-hub |
| --- | --- | --- |
| 核心职责 | 把人和本地 Agent 连接到聊天平台 | 让本地 Agent 之间互相发现和委派 |
| 入口 | 聊天应用命令、群聊消息、Bot 绑定 | Agent 内部调用 CLI / Skill |
| 目标选择 | 人决定要找哪个 Bot | Agent 运行 `cc-relay-hub list` 动态发现 |
| 请求跟踪 | 偏聊天历史和会话管理 | SQLite 记录 request id、session lock、回复归属 |
| 回复路径 | 回到聊天平台当前对话 | 回到发起 Agent 的原始 session |
| 等待方式 | 人看聊天窗口 | `send --wait` 和 `watch`，适合 Agent 工具调用 |

实际好处是：一个 Agent 可以自己完成可靠的任务交接。

- 不硬编码 Bot 名称，先发现当前可用 peer
- 发送前检查目标 webhook、session、健康状态
- 同一目标 session 一次只保留一个待回复请求，避免消息串线
- 可以等待匹配回复，不需要 `tail -f` 或 `while true` 这种会卡住 Agent 的 shell 循环
- 目标 Agent 的回复会回到发起方所在的原始 cc-connect session

所以它更适合 planner-builder、reviewer-implementer、researcher-coder 这类“一个 Agent 需要把任务交给另一个 Agent，并拿到明确结果再继续”的工作流。

## 工作原理

```text
聊天应用
   |
   v
cc-connect project A  -- cc-relay-hub send -->  cc-connect project B
   ^                                                |
   |                                                v
   +------- Hook Server 接收 message.sent <---------+
            并把回复推回 project A
```

1. Agent A 调用 `cc-relay-hub send <agent> "<task>"`
2. cc-relay-hub 从本地 cc-connect 配置和 session 文件发现 Agent B
3. 任务通过 Agent B 的本地 cc-connect webhook 投递
4. Agent B 回复后，cc-connect 触发 `message.sent` hook
5. cc-relay-hub 匹配原始 request，把回复推回 Agent A 的 session

## 快速接入

### 让 AI Agent 自动安装配置

推荐方式不是手动改 TOML，而是直接把下面这段发给 Claude Code、Codex、Gemini CLI、Cursor Agent 或其他本地 Coding Agent：

```text
Follow https://raw.githubusercontent.com/fengjunchengCode/cc-relay-hub/refs/heads/main/INSTALL.md to install and configure cc-relay-hub.

Do not ask me to edit config files by hand. Check prerequisites, install the CLI wrapper, enable the required cc-connect webhooks and message.sent hooks, restart services when needed, start the hook server, verify cc-relay-hub list, and run an end-to-end send test.
Ask me only for values you cannot safely infer, such as which cc-connect projects should participate.
```

`INSTALL.md` 是给 AI Agent 执行的安装清单：检查依赖、安装 CLI、写入必要配置、重启服务、启动 Hook Server、验证发现和端到端发送。正常情况下，用户只需要告诉 Agent 哪些 cc-connect 项目要参与。

### 手动兜底

如果你想自己检查每一步，可以手动执行：

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

每个参与的 cc-connect 配置都需要：

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

多个 cc-connect 进程请使用不同 webhook 端口。修改后重启 cc-connect，并启动 relay Hook Server：

```bash
node ~/.cc-connect/cc-relay-hub/hook-server.mjs
```

## 验证

```bash
cc-relay-hub list
cc-relay-hub info <agent>
cc-relay-hub send <agent> "Ping from another agent" --wait --timeout 120
```

`Session` 不应为 `none`。如果是 `none`，打开该 Bot 自己的聊天窗口，发送一条普通消息，然后重新运行 `cc-relay-hub list`。

## CLI

| 命令 | 说明 |
| --- | --- |
| `cc-relay-hub list` | 发现本机已配置 Agent |
| `cc-relay-hub list --format json` | 给 Agent Skill 使用的机器可读 peer 列表 |
| `cc-relay-hub info <agent>` | 查看 webhook、session、健康状态 |
| `cc-relay-hub send <agent> "<msg>"` | 向 peer Agent 发送消息 |
| `cc-relay-hub send <agent> "<msg>" --wait` | 发送并等待匹配回复 |
| `cc-relay-hub watch` | 单次长轮询获取新 hook 事件 |
| `cc-relay-hub watch --loop` | 给人类终端或 tmux 使用的持续事件流 |

## Agent Skill

仓库内置通用 relay skill：

```text
skills/cc-relay.md
```

Claude Code：

```bash
mkdir -p ~/.claude/skills
cp ~/.cc-connect/cc-relay-hub/skills/cc-relay.md ~/.claude/skills/
```

其他 Agent 可以把同一个文件加载到自己的项目指令或 Skill 系统里。这个 Skill 会通过 `cc-relay-hub list --format json` 实时发现 peer，不会硬编码 Agent 名称。

## 架构

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

- **Discovery**：读取本地 `~/.cc-connect/config*.toml` 和 session 元数据
- **Provider**：通过 cc-connect 本地 webhook 投递消息
- **State**：记录 request id、session lock、投递状态、回复、通知状态
- **Hook Server**：接收 `message.sent` 事件，把匹配回复推回发起方 session

## 运行注意

- 发起方和目标方都要启用 `[webhook]`
- 需要回传回复的项目要添加 `message.sent` hook
- 每个 Bot 首次使用前，先在它自己的聊天窗口发送一条普通消息完成 session 初始化
- 不要用 `tail -f`、`while true` 或 shell 轮询循环看事件；使用 `send --wait` 或 `watch`
- Hook Server 默认监听 `127.0.0.1:9120`

## 完整安装指南

见 [INSTALL.md](INSTALL.md)。它按 AI Coding Agent 可执行的方式编写，每一步都有检查和验证。

## License

MIT
