# cc-relay-hub

<p align="center">
  <img src="https://raw.githubusercontent.com/fengjunchengCode/cc-relay-hub/main/docs/assets/cc-relay-hub-readme-hero.png" alt="cc-relay-hub README 头图：本地 relay hub 小终端在手机和本地 AI agent 节点之间转发消息" width="920">
</p>

<p align="center">
  <strong>让本地 AI coding agent 互相派活、互相回信，不再让你在聊天窗口之间复制粘贴。</strong>
</p>

<p align="center">
  <a href="https://github.com/fengjunchengCode/cc-relay-hub/releases"><img alt="version" src="https://img.shields.io/github/v/release/fengjunchengCode/cc-relay-hub?label=version"></a>
  <a href="#-contributing--license--star-history--acknowledgments"><img alt="license" src="https://img.shields.io/badge/license-MIT-yellow"></a>
  <a href="https://github.com/chenhg5/cc-connect"><img alt="built with cc-connect" src="https://img.shields.io/badge/built%20with-cc--connect-blue"></a>
  <a href="./README.md"><img alt="English README" src="https://img.shields.io/badge/README-English-blue"></a>
</p>

<p align="center">
  <a href="./README.md">English</a> | 中文
</p>

**你在手机上指挥本地 AI coding agent，cc-relay-hub 让这些 agent 私下互相派活、互相回信。**

cc-connect 已经解决了第一步：你可以在飞书、微信、Telegram、Slack 等聊天入口里操作 Claude Code、Codex、Gemini CLI 或其他本地 agent。真正麻烦的是同时跑多个 agent 之后：Claude 想把实现交给 Codex，Hermes 想给多个 agent 分发任务，IDE agent 又要把结果回传回来。

cc-relay-hub 补上的就是这层安静的 agent-to-agent 交接。agent 可以发现同伴、发送任务、等待回复，并把答案带回最开始的对话。

<p align="center">
  <strong>本地优先</strong> · <strong>agent agnostic</strong> · <strong>安静交接</strong> · <strong>自动闭环回信</strong>
</p>

## 🆕 重大更新：跨电脑的 agent 协作（cross-node federation）

<p align="center">
  <img src="https://raw.githubusercontent.com/fengjunchengCode/cc-relay-hub/main/docs/assets/cc-relay-hub-cross-node-hero.png" alt="cc-relay-hub 跨机器：家里的笔记本和公司的台式机各自运行 Claude 和 Codex，通过局域网直连或加密中继相连" width="920">
</p>

你的 agent 不必再挤在一台机器上。每台电脑跑一个 **节点守护进程（消息中心）**，集群里任何机器上的 agent 都用 `name@node` 来称呼：

```bash
cc-relay-hub node install                 # 每台电脑执行一次
cc-relay-hub node invite                  # 在已入群的电脑上执行，得到 ccrelay://join/...
cc-relay-hub node join "ccrelay://join/…" # 在新电脑上执行（把这串发给它的 agent 就行）

cc-relay-hub list                         # 远端 agent 显示为 codex-bot@office
cc-relay-hub send codex-bot@office "按方案实现并跑完测试"
```

- 🔌 **零网络配置** — 不用 VPN、不开端口、不注册账号。机器之间通过集合点上的加密地址卡互相发现（默认公共 ntfy.sh，也可以用自带的 `relay_server.py` 自托管）。
- 🛣️ **自动选最优路径** — 同一局域网走直连，毫秒级；任一方有可达地址也走直连；否则走加密中继。每分钟重新探测，自动升降级。
- 🔐 **端到端加密** — 所有离开本机的信封都用集群密钥派生的密钥加密和签名。集合点只见密文，未签名的请求一律 `401`。
- 🔁 **回信闭环不变** — 请求由目标电脑自己的守护进程投递进 agent 会话（含忙碌检查和投递确认），回复由发起电脑的守护进程注入回原会话。对方离线时 `send` 显示 `queued`，守护进程最长重试 48 小时。
- 🧭 **agent 自己就会用** — 生成的 CLAUDE.md / AGENTS.md 会把远端 agent 列成 `name@node`；`cc-relay-hub node doctor` 会指出任何连通性问题和修法。

详情见 [docs/federation.md](./docs/federation.md)。本机原有用法完全不变。

## ✨ Why cc-relay-hub?

- 📱 <strong>手机上指挥 agent</strong> — 保留 cc-connect 的移动端入口，再让本地 agent 接着互相协作。
- 🤫 <strong>不把群聊炸掉</strong> — 你可以继续单独和每个 agent 对话，不必把所有 bot 拉群 @。
- 🔁 <strong>回信自动闭环</strong> — Claude 把任务交给 Codex，Codex 做完后结果回到 Claude 当前会话。
- 🧭 **agent 自己找同伴** — `cc-relay-hub list` 实时列出 peer，生成的项目指令会教新 agent 怎么使用这些同伴。
- 🧩 **CLI agent 和 IDE agent 都能接入** — Claude Code / Codex 走 cc-connect，Antigravity 这类 Electron IDE 可走 CDP。
- ⏳ **不阻塞地闭环回复** — 普通 `send` 会立刻返回，hook server 会把带标记的回复转回发起会话。
- 📥 **发起会话忙时也不丢回复** — 回复先进入持久 outbox；Hub 在 POST 前检查持久化回合状态，只在发起 agent 当前回合结束后重投一次。

## 🛤️ Why I built this

我做 cc-relay-hub，是因为我每天通勤时都在用 cc-connect。手机上用飞书或微信操作本地 agent 很方便，但多 agent 交接还得靠复制粘贴，或者把 bot 都拉进群里让消息爆炸。我想保留一对一聊天的安静体验，同时让 agent 有一条可控的私下协作通道。

<!-- TODO: 作者可以把这里替换成更具体的通勤故事。 -->

## 🆚 cc-relay-hub 和 cc-connect 多机器人聊天

| 当你想做什么 | cc-connect 多机器人聊天 | cc-relay-hub |
|---|---|---|
| 在手机上操作本地 agent | 很适合 | 继续保留这个体验 |
| 单独和某个 agent 对话 | 很适合 | 继续保持会话分离 |
| 让 Claude 把任务交给 Codex | 你复制粘贴，或在群里 @ | `send` 直接交接并立刻返回 |
| 让多个 agent 分工 | 群聊很容易变吵 | Hermes 可以安静分发 |
| 等另一个 agent 回复 | 人盯着聊天窗口 | hook server 转发带标记的回复，不阻塞发起 agent |
| 把 IDE agent 纳入团队 | 不是主要路径 | CDP 可以接入 Antigravity / Cursor |

## 🚀 Quick Start

cc-relay-hub 是一个本地 CLI + 一个很小的 hook server，二者都运行在 127.0.0.1。

### 方案 A：让 AI agent 帮你装

把这段 prompt 丢给 Claude Code、Codex、Cursor Agent 或其他本地 coding agent：

```text
Follow https://raw.githubusercontent.com/fengjunchengCode/cc-relay-hub/refs/heads/main/INSTALL.md to install and configure cc-relay-hub.
```

安装指南是给 agent 执行的。它会检查依赖、复用 cc-connect 的自动化配置、写入本地 relay 配置、启动 hook server，并验证第一条路由。

### 方案 B：手动安装

<details>
<summary>展开手动安装步骤</summary>

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

如果你还没配置过 cc-connect，先看 [INSTALL.md](./INSTALL.md)。运行细节见 [docs/operations.md](./docs/operations.md)。

</details>

最常用命令：

```bash
cc-relay-hub bootstrap
cc-relay-hub list
cc-relay-hub send <agent> "task"
cc-relay-hub send <agent> "status update" --no-reply
cc-relay-hub inbox
cc-relay-hub relay <from-agent> <to-agent> "task"  # 仅用于同步诊断交接
cc-relay-hub watch
cc-relay-hub node install | status | invite | join <string> | doctor   # 跨电脑
cc-relay-hub send <agent>@<node> "task"                                 # 发给另一台电脑上的 agent
```

私信默认是异步的：发出任务后，目标 agent 带标记的回复会由 hook server 转回发起会话。如果发起会话还在思考，回复会保存在 outbox，Hub 不会定时反复撞 busy session；等该回合结束产生明确 idle edge 后只重投一次。`cc-relay-hub inbox` 可查看尚未确认送达的回复。除非用户明确要求同步诊断等待，否则 agent 会话里不要开启监听，也不要用 `--wait` 等回复。

通道必须先分清：用户要求在已绑定群聊中公开讨论时，使用
`cc-connect relay send`；需要私下委派时，才使用 `cc-relay-hub send`。
如果来源是命名群聊，Hub 默认拒绝 `send`，只有用户明确要求私下委派时才允许
加 `--private`，避免把公开讨论静默切换到私信通道。

## 💬 典型场景

### 1. Claude 把实现任务 relay 给 Codex

你在 Claude Code 里完成方案设计，然后把实现交给 Codex：

```bash
cc-relay-hub send codex "Implement this plan and run the tests"
```

Codex 在自己的会话里工作，结果回到 Claude 当前对话。你继续在 Claude 里推进思路，Codex 负责落地 patch。

<!-- TODO: 截图/GIF：Claude Code 执行 `cc-relay-hub relay ...`，Codex 回复，结果回到 Claude。建议路径：docs/assets/demo-relay-claude-codex.gif -->

### 2. Hermes 调度多个 agent 并行派活

Hermes 可以发现当前本机的 agent 网络，按名字或 group 选择目标，再把不同任务发给不同同伴。一个 agent 写代码，一个 agent 审阅，另一个 agent 查文档或看日志。

```bash
cc-relay-hub list --format json
cc-relay-hub send codex "Implement the parser change"
cc-relay-hub send claude "Review the patch for regressions"
```

重点不是更吵的群聊，而是一个安静的调度者，把每个 agent 的工作放到正确的 lane 里。

<!-- TODO: 截图：Hermes 列出 grouped agents，并给多个 peer 分发任务。建议路径：docs/assets/screenshot-hermes-dispatch.png -->

### 3. Hermes 操作 Antigravity IDE 并收到回复

有些任务必须在 IDE 里完成。通过 CDP-backed agent，Hermes 可以把任务送进 Antigravity，让 IDE agent 执行，再把回复收回同一个 relay 工作流。

```bash
cc-relay-hub cdp status antigravity-ide
cc-relay-hub send antigravity-ide "Open the project and inspect the failing workflow"
```

这样 Electron IDE agent 也能和 Claude Code、Codex 一样进入本地协作网络。

<!-- TODO: 截图/GIF：Hermes 通过 CDP 给 Antigravity 发送任务并收到回复。建议路径：docs/assets/demo-antigravity-cdp.gif -->

### 4. 把任务交给另一台电脑上的 agent

家里的笔记本上 Claude 负责规划，公司台式机上的 Codex 有完整代码库和测试环境：

```bash
cc-relay-hub list                                   # codex-bot@office  codex  [team]  online  lan
cc-relay-hub send codex-bot@office "按 PLAN.md 实现，并跑完整测试"
```

公司机器的守护进程把任务送进 Codex 自己的会话，Codex 用 relay 标记作答，答案出现在笔记本上发起的那个对话里。台式机睡着了也没关系，请求留在 outbox，醒来就送。

## 🧩 支持矩阵

| Agent / tool | 路径 | 状态 | 适合做什么 |
|---|---|:---:|---|
| Claude Code | cc-connect | ✅ | 规划、审阅、长对话 coding |
| Codex | cc-connect | ✅ | 实现、测试、聚焦 patch |
| Gemini CLI | cc-connect | ✅ | research、备选审阅、总结 |
| Cursor | cc-connect 或 CDP | ⚠️ | IDE 工作流，成熟度取决于本地配置 |
| Antigravity | CDP | ✅ | 通过本地 Electron 窗口做 IDE automation |
| Hermes | 本地调度者 | ✅ | 发现 peer、派发任务、收集回复 |
| 其他 Electron IDE | CDP backend | ⚠️ | 需要能找到对应 IDE 的 chat UI |
| Windows | local CLI + 节点守护进程 | ✅ | 可作为集群成员运行（已验证：一台 Windows 台式机和一台 macOS 节点互发任务与回复）。 |
| 其他电脑 | 节点守护进程（`name@node`） | ✅ | 加密的跨机器投递，自动在局域网直连 / 中继之间切换；见 [docs/federation.md](./docs/federation.md)。 |

## ❓ FAQ

<details>
<summary><strong>它和 cc-connect 什么关系？</strong></summary>

cc-connect 负责把人通过聊天应用连接到本地 agent。cc-relay-hub 建在这个工作流之上，让本地 agent 之间也能私下交接任务。

</details>

<details>
<summary><strong>必须装 cc-connect 吗？</strong></summary>

Claude Code、Codex、Gemini CLI 这类聊天入口 agent 推荐通过 cc-connect 接入。CDP IDE agent 可以单独加入，但最顺的路径还是从 cc-connect 开始。

</details>

<details>
<summary><strong>CDP 安全吗？</strong></summary>

CDP 可以控制 IDE 窗口。只把调试端口绑定在 localhost，不要暴露到网络，不需要 IDE automation 时及时关闭。

</details>

<details>
<summary><strong>支持 Windows 吗？</strong></summary>

支持。CLI、hook server 和节点守护进程都能在 Windows（Python 3.9+）上运行，`cc-relay-hub node install` 会把守护进程注册为登录任务。已经验证过一台 Windows 台式机作为集群成员与 macOS 节点互发任务和回复。

</details>

<details>
<summary><strong>能跨机器用吗？</strong></summary>

能，节点守护进程就是为此而生。每台电脑执行 `cc-relay-hub node install`，用一个邀请串把它们连起来，然后用 `name@node` 称呼远端 agent。不需要 VPN 也不用开端口：不同网络的机器通过加密中继通信，能直连时自动切换到直连。见 [docs/federation.md](./docs/federation.md)。

</details>

<details>
<summary><strong>中继服务器能看到什么？</strong></summary>

只有一个猜不到的主题名和密文。信封在离开本机前就已经用集群密钥派生的密钥加密并签名，公共 ntfy.sh（或你自托管的 `relay_server.py`）既读不到内容，也伪造不了消息。

</details>

## 📖 进阶文档

- [Architecture](./docs/architecture.md) — 内部流程、图示、路由生命周期
- [Providers](./docs/providers.md) — cc-connect 和 CDP provider 配置
- [CLI Reference](./docs/cli.md) — 完整命令列表
- [Operations](./docs/operations.md) — hook server、session 初始化、排障
- [跨机器联邦](./docs/federation.md) — 其他电脑、`name@node`、路径选择、安全、自托管中继
- [AI-agent install guide](./INSTALL.md) — 给 agent 执行的安装步骤

## 🤝 Contributing / License / Star History / Acknowledgments

欢迎贡献新的 CDP backend、provider adapter、真实截图、工作流案例和文档修正。

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

cc-relay-hub 基于 [cc-connect](https://github.com/chenhg5/cc-connect)，也来自 Claude Code、Codex、Hermes、Antigravity 等真实多 agent coding 工作流。

<!-- TODO: 下次公开发布前补齐真实截图/GIF。 -->
