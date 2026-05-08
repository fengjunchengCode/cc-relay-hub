# cc-relay-hub

<p align="center">给本地 AI Agent、cc-connect Bot 和 CDP 控制的 Electron IDE 加一层调度中枢。</p>

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

它不是 cc-connect 的替代品，而是一层本地 Agent 调度层：既可以基于 cc-connect webhook / hook 投递，也可以通过 Chrome DevTools Protocol 控制 Electron IDE。

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
| IDE 覆盖 | 已接入 cc-connect 的 Agent | cc-connect Agent 加本地 CDP 暴露的 Electron IDE |

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

CDP Agent 走同一个 relay core，只是 provider 不同：

```text
Agent / Skill  -- cc-relay-hub send -->  CDP provider  -- WebSocket -->  Electron IDE
     ^                                      |
     +--------- marker 匹配回复 <-----------+
```

CDP provider 会把 relay prompt 输入到 IDE，轮询 DOM transcript，并从回答里提取 `[cc-relay reply_to=...]` marker。CDP 回复不依赖 Hook Server。

## Provider

| Provider | 传输方式 | 回复检测 | 适用场景 |
| --- | --- | --- | --- |
| `cc_connect` | 本地 HTTP webhook | `message.sent` Hook Server | 已经通过 cc-connect 接入的 Claude Code、Codex 或其他 Agent |
| `cdp` | Chrome DevTools Protocol WebSocket | DOM transcript 轮询加 relay marker | Antigravity、Cursor 等本地 Electron IDE |

CDP agent 示例：

```json
{
  "agents": {
    "antigravity-ide": {
      "type": "antigravity",
      "provider": "cdp",
      "work_dir": "/path/to/project",
      "capabilities": ["message.send", "session.control"],
      "labels": ["ide"]
    }
  }
}
```

```json
{
  "cdp": {
    "antigravity-ide": {
      "backend": "antigravity",
      "cdp_port": 9000
    }
  }
}
```

CDP 端口必须只绑定在本机。暴露 CDP 端口等同于把 IDE 窗口交给自动化控制。

## Groups 与 Relay

Groups 提供通信编组，是弱隔离模式：同组 Agent 可以互相 relay；为了向后兼容，没有加入任何 group 的 Agent 仍允许通信。

```bash
cc-relay-hub groups create core --description "Main project agents"
cc-relay-hub groups join core claudecode
cc-relay-hub groups join core codex-bot
cc-relay-hub groups show core
```

当一个 Agent 需要以自己的身份向另一个 Agent 派发任务并等待回复时，用 `relay`：

```bash
cc-relay-hub relay claudecode codex-bot "Review the failing test and suggest a fix" --timeout 120
```

`relay` 总是等待回复。如果发起方是带 session key 的 cc-connect provider，回复还会被推回发起方的原始 session。

## Bootstrap Context

`bootstrap context` 会根据当前 registry 自动生成各 Agent 的原生上下文文件，让用户 clone 项目后无需手写说明，Agent 也能知道如何发现 peer、检查健康状态和遵守 relay protocol。

```bash
cc-relay-hub bootstrap context --print
cc-relay-hub bootstrap context --check
cc-relay-hub bootstrap context --write
```

生成文件包括 `AGENTS.md`、`CLAUDE.md`、`GEMINI.md` 和 `.cursorrules`。当 registry 里有多个 Agent 时，per-agent 文件会写入 `.cc-relay-hub/<agent>/`，避免不同 Agent 拿到错误身份。

## 快速接入

### 让 AI Agent 自动安装配置

推荐方式不是手动改 TOML，而是直接把下面这段发给 Claude Code、Codex、Gemini CLI、Cursor Agent 或其他本地 Coding Agent：

```text
Follow https://raw.githubusercontent.com/fengjunchengCode/cc-relay-hub/refs/heads/main/INSTALL.md to install and configure cc-relay-hub.
```

`INSTALL.md` 是给 AI Agent 执行的全自动安装清单。安装过程中会优先使用 cc-connect 的自动化配置手段：

- `cc-connect web` — 浏览器可视化配置所有平台，无需手动编辑 TOML
- `cc-connect feishu setup` — 扫码创建飞书应用，自动写入配置
- `cc-connect weixin setup` — 扫码登录个人微信

检查依赖、自动写入 hook 和 webhook 配置、重启服务、启动 Hook Server、运行 bootstrap 验证连通性。全程无需用户手动编辑配置文件。

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
| `cc-relay-hub bootstrap` | 扫描配置、生成 registry、验证连通性 |
| `cc-relay-hub bootstrap context --write` | 生成 AGENTS.md 和各 Agent 原生上下文文件 |
| `cc-relay-hub list` | 发现本机已配置 Agent |
| `cc-relay-hub list --format json` | 给 Agent Skill 使用的机器可读 peer 列表 |
| `cc-relay-hub info <agent>` | 查看 webhook、session、健康状态 |
| `cc-relay-hub send <agent> "<msg>"` | 向 peer Agent 发送消息 |
| `cc-relay-hub send <agent> "<msg>" --wait` | 发送并等待匹配回复 |
| `cc-relay-hub groups` | 查看和管理 Agent groups |
| `cc-relay-hub relay <from> <to> "<msg>"` | 发起组内 Agent-to-Agent 请求并等待回复 |
| `cc-relay-hub cdp status <agent>` | 检查 CDP Electron IDE Agent |
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
             ┌──────────────┬──────────────┐
             │ Hook Server  │ CDP Backend  │
             │ :9120        │ Electron IDE │
             └──────────────┴──────────────┘
```

- **Discovery**：读取本地 `~/.cc-connect/config*.toml` 和 session 元数据
- **Provider**：通过 cc-connect 本地 webhook 或本地 CDP WebSocket 投递消息
- **State**：记录 request id、session lock、投递状态、回复、通知状态
- **Hook Server**：接收 `message.sent` 事件，把匹配回复推回发起方 session
- **Bootstrap Context**：生成项目内 Agent 指令文件，让 Agent 自动获得 peer 列表和 relay protocol

## 运行注意

- 新增 cc-connect 实例后，运行 `cc-relay-hub bootstrap` 重新扫描并验证连通性
- 发起方和目标方都要启用 `[webhook]`
- 需要回传回复的项目要添加 `message.sent` hook
- 每个 Bot 首次使用前，先在它自己的聊天窗口发送一条普通消息完成 session 初始化
- CDP 调试端口只应监听 `127.0.0.1`，不需要时及时关闭
- 不要用 `tail -f`、`while true` 或 shell 轮询循环看事件；使用 `send --wait` 或 `watch`
- Hook Server 默认监听 `127.0.0.1:9120`

## 完整安装指南

见 [INSTALL.md](INSTALL.md)。它按 AI Coding Agent 可执行的方式编写，每一步都有检查和验证。

## License

MIT
