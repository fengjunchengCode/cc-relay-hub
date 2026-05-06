# cc-relay-hub

<p align="center">在任意本地 AI Agent 之间路由消息。从任意聊天应用发起。</p>

<p align="center">
  中文 · <a href="https://github.com/fengjunchengCode/cc-relay-hub/blob/main/README.md">English</a>
</p>

---

## 简介

**cc-relay-hub** 是基于 [cc-connect](https://github.com/chenhg5/cc-connect) 构建的轻量级消息中继。它让任意 AI Agent（Claude Code、Codex、Gemini CLI 等）能够通过统一的 CLI 相互发现和发送消息。

**典型场景**：从一个 Agent 委派任务给另一个 Agent。例如，让 Claude Code 规划架构，然后将设计文档发送给 Codex 实现——全程在聊天窗口中完成。

> **仅限单机使用。** cc-relay-hub 运行在一台机器上。它读取本地 cc-connect 配置文件、使用本地 webhook 端点，Hook Server 绑定 `127.0.0.1`。它不是跨机器的消息总线。

## 工作原理

```
聊天应用 ──► cc-connect (Agent A) ──► cc-relay-hub send ──► cc-connect (Agent B)
                  ▲                                              │
                  │         hook: message.sent                   │
                  └──────────────────────────────────────────────┘
```

1. Agent A 通过 webhook 向 Agent B 发送消息
2. Agent B 回复后，cc-connect 触发 `message.sent` 钩子
3. Hook Server 接收事件，通过 Agent A 自己的 cc-connect 实例将回复推回聊天窗口

## 快速开始

### 前置条件

- 已安装并配置 [cc-connect](https://github.com/chenhg5/cc-connect)
- Python 3.9+
- Node.js（用于 Hook Server）
- 至少配置了两个 cc-connect 项目

### 安装

```bash
# 克隆仓库
git clone https://github.com/fengjunchengCode/cc-relay-hub.git ~/.cc-connect/cc-relay-hub

# 添加 CLI 到 PATH（bash 用户请将 .zshrc 替换为 .bashrc）
mkdir -p ~/bin
ln -sf ~/.cc-connect/cc-relay-hub/bin/cc-relay-hub ~/bin/cc-relay-hub
echo 'export PATH="$HOME/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

### 配置

**1. 在所有参与项目上启用 webhook**（调用方和目标方都需要）：

```toml
# 每个项目的 cc-connect 配置文件
[webhook]
enabled = true
port = 9110    # 每个项目使用不同端口
path = "/hook"
```

**2. 在每个目标项目上添加回复钩子**：

```toml
[[hooks]]
event = "message.sent"
type = "http"
url = "http://127.0.0.1:9120/cc-connect/hooks/reply"
async = false
timeout = 2
```

**3. 修改配置后重启 cc-connect。**

**4. 初始化会话**：打开每个机器人的聊天窗口，发送一条普通消息。这会创建 `cc-relay-hub list` 发现所需的初始会话。

**5. 启动 Hook Server**：

```bash
node ~/.cc-connect/cc-relay-hub/hook-server.mjs
```

macOS 开机自启请参阅 [INSTALL.md](INSTALL.md)。

### 验证

```bash
# 发现已配置的 Agent（Session 不应为 "none"）
cc-relay-hub list

# 发送消息
cc-relay-hub send codex-bot "来自 Claude Code 的问候"

# 发送并等待回复
cc-relay-hub send codex-bot "请审阅这段代码" --wait --timeout 120
```

## CLI 命令

| 命令 | 说明 |
|------|------|
| `cc-relay-hub list` | 发现所有已配置的 Agent |
| `cc-relay-hub info <agent>` | 查看 Agent 详情、Webhook 健康状态、会话状态 |
| `cc-relay-hub send <agent> "<msg>"` | 向 Agent 发送消息 |
| `cc-relay-hub send <agent> "<msg>" --wait` | 发送并阻塞等待回复 |
| `cc-relay-hub watch` | 单次长轮询获取新钩子事件（Agent 可安全使用） |
| `cc-relay-hub watch --loop` | 持续事件流（适合人类终端 / tmux） |

## 架构

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

- **注册/发现**：读取 cc-connect 配置文件发现 Agent
- **传输层**：通过 cc-connect webhook 端点投递消息
- **状态存储**：SQLite 跟踪消息生命周期和回复归属
- **Hook Server**：接收 `message.sent` 事件，通过发起方自己的 cc-connect 路径推回回复

## 重要说明

- **所有参与项目**都必须启用 `[webhook]`——包括发起方（调用方）项目
- **使用前请先初始化每个机器人**：在聊天窗口发送一条普通消息
- **禁止使用 shell 轮询循环**（`tail -f`、`while true`）监控事件——请使用 `cc-relay-hub watch`（单次，Agent 可安全使用）或 `cc-relay-hub send --wait`（请求/回复）
- Hook Server 默认监听 `127.0.0.1:9120`

## 详细安装指南

请参阅 [INSTALL.md](INSTALL.md) 获取分步指南（专为 AI Agent 自动执行设计）。

## 许可证

MIT
