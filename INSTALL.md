# cc-relay-hub Installation Guide

This guide is written for AI coding agents to execute step by step. Do not skip failed checks.

## What this installs

`cc-relay-hub` is a thin relay layer on top of `cc-connect`.

- Discovery: reads local `cc-connect` config files
- Send path: posts to each agent's local webhook
- Reply path: receives `message.sent` hooks on `127.0.0.1:9120`
- Phase 1a lock: only one pending outbound write per target `session_key`

Current CLI:

```bash
cc-relay-hub list
cc-relay-hub info <agent>
cc-relay-hub send <agent> "<message>"
cc-relay-hub send <agent> "<message>" --wait --timeout 300
```

`registry.json` and `bindings.json` are auto-generated on the first `list` or `info` run. There is no `bootstrap` command.

## Prerequisites

Run all checks before installation.

### 1. `cc-connect` must be installed

```bash
cc-connect --version
```

If this fails:

```bash
npm install -g cc-connect
```

or

```bash
brew install cc-connect
```

Official install guide:

`https://github.com/chenhg5/cc-connect/blob/main/INSTALL.md`

### 2. `git` must be installed

```bash
git --version
```

If this fails on macOS, install Xcode Command Line Tools:

```bash
xcode-select --install
```

### 3. `python3` 3.9+ must be available

```bash
python3 --version
```

If this fails:

```bash
brew install python3
```

### 4. `node` must be available for the hook server

```bash
node --version
```

If this fails:

```bash
brew install node
```

### 5. At least one `cc-connect` config file must exist

```bash
find ~/.cc-connect -maxdepth 1 -name 'config*.toml' -print
test -f /opt/homebrew/etc/cc-connect/config.toml && echo /opt/homebrew/etc/cc-connect/config.toml
```

If nothing is printed, configure `cc-connect` first.

### 6. Target projects must have webhook enabled

Run:

```bash
python3 - <<'PY'
from pathlib import Path
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

paths = [
    Path.home() / ".cc-connect" / "config.toml",
    Path.home() / ".cc-connect" / "config-codex.toml",
    Path("/opt/homebrew/etc/cc-connect/config.toml"),
]
found = False
for path in paths:
    if not path.exists():
        continue
    with path.open("rb") as handle:
        config = tomllib.load(handle)
    webhook = config.get("webhook", {})
    print(f"{path}: enabled={webhook.get('enabled')} port={webhook.get('port')}")
    if webhook.get("enabled") and webhook.get("port"):
        found = True
raise SystemExit(0 if found else 1)
PY
```

If this exits non-zero, add a valid `[webhook]` block to each target project before using `send`:

```toml
[webhook]
enabled = true
port = 9112
path = "/hook"
```

### 7. Each target bot should already have one live chat session

This cannot be verified until after `cc-relay-hub` is cloned. The check happens in Step 4. If a `session_key` is empty or `none`, send one normal message to that bot in its own chat window, then rerun discovery.

## Step 1: Clone the repository

```bash
git clone git@github.com:fengjunchengCode/cc-relay-hub.git ~/.cc-connect/cc-relay-hub
```

Verify:

```bash
test -f ~/.cc-connect/cc-relay-hub/hub.py
test -x ~/.cc-connect/cc-relay-hub/bin/cc-relay-hub
test -d ~/.cc-connect/cc-relay-hub/core
test -d ~/.cc-connect/cc-relay-hub/providers
test -d ~/.cc-connect/cc-relay-hub/tests
```

## Step 2: Install the CLI wrapper

The repo includes a wrapper at `~/.cc-connect/cc-relay-hub/bin/cc-relay-hub`.

To expose it on `PATH`:

```bash
mkdir -p ~/bin
ln -sf ~/.cc-connect/cc-relay-hub/bin/cc-relay-hub ~/bin/cc-relay-hub
case ":$PATH:" in
  *":$HOME/bin:"*) ;;
  *) echo 'export PATH="$HOME/bin:$PATH"' >> ~/.zshrc; export PATH="$HOME/bin:$PATH" ;;
esac
hash -r
```

Verify:

```bash
command -v cc-relay-hub
cc-relay-hub list
```

The first `list` run auto-generates `registry.json` and `bindings.json` if they do not exist.

## Step 3: Configure `message.sent` hooks

Add this to each `cc-connect` config that backs an agent you want to relay through:

```toml
[[hooks]]
event = "message.sent"
type = "http"
url = "http://127.0.0.1:9120/cc-connect/hooks/reply"
async = false
timeout = 2
```

Use `async = false` here because reply monitoring depends on hook delivery. The local hook handler is lightweight and returns quickly; that is the expected mode for this setup.

Restart each affected `cc-connect` process after editing its config.

Examples:

```bash
launchctl kickstart -k gui/$(id -u)/com.cc-connect.codex
```

## Step 4: Verify discovery and live sessions

Run:

```bash
cc-relay-hub list
```

Expected:

- agents are listed
- `Webhook` is not `none` for target agents
- `Session` is not `none`

If `Session` is `none` or empty:

1. Open that bot's own chat window
2. Send one normal message manually
3. Rerun `cc-relay-hub list`

If `Webhook` is `none`, fix the target project's `[webhook]` config and rerun `list`.

## Step 5: Start the hook server

### Manual start

```bash
node ~/.cc-connect/cc-relay-hub/hook-server.mjs
```

### macOS launchd

Generate the plist with the actual Node path:

```bash
NODE_BIN="$(command -v node)"
cat > ~/Library/LaunchAgents/com.cc-relay-hub.hook.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.cc-relay-hub.hook</string>
  <key>ProgramArguments</key>
  <array>
    <string>${NODE_BIN}</string>
    <string>${HOME}/.cc-connect/cc-relay-hub/hook-server.mjs</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/cc-relay-hub-hook.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/cc-relay-hub-hook.log</string>
</dict>
</plist>
EOF
launchctl unload ~/Library/LaunchAgents/com.cc-relay-hub.hook.plist 2>/dev/null || true
launchctl load ~/Library/LaunchAgents/com.cc-relay-hub.hook.plist
```

Verify the listener precisely:

```bash
lsof -nP -iTCP:9120 -sTCP:LISTEN | grep node
```

## Step 6: Verify end-to-end hooks

Use a unique token so validation does not read an old event:

```bash
EVENT_FILE=~/.cc-connect/cc-relay-hub/hook-events.jsonl
TOKEN="relay-hook-$(date +%s)"
BEFORE=$(wc -l < "$EVENT_FILE" 2>/dev/null || echo 0)
cc-relay-hub send <agent-name> "ping test ${TOKEN}"
sleep 5
AFTER=$(wc -l < "$EVENT_FILE" 2>/dev/null || echo 0)
test "$AFTER" -gt "$BEFORE"
tail -n 5 "$EVENT_FILE" | grep "$TOKEN"
```

If the final `grep` fails, the hook did not deliver the current event.

## Step 7: Install the relay skill

The repo ships with a generic skill file:

`~/.cc-connect/cc-relay-hub/skills/cc-relay.md`

For Claude Code:

```bash
mkdir -p ~/.claude/skills
cp ~/.cc-connect/cc-relay-hub/skills/cc-relay.md ~/.claude/skills/
```

For other agents, load the same file into their local instruction/skill mechanism. The file does not hardcode peer names; it always discovers current agents with `cc-relay-hub list --format json`.

## Troubleshooting

### `cc-relay-hub list` shows no agents

- Confirm Step 5 prerequisite config files exist
- Rerun `cc-relay-hub list` from the installed wrapper, not from an old shell alias
- If needed, inspect `~/.cc-connect/config*.toml` and `/opt/homebrew/etc/cc-connect/config.toml`

### `cc-relay-hub list` shows `Session` as `none`

Open that bot's chat window, send one normal message, then rerun `cc-relay-hub list`.

### Hook events not appearing in `hook-events.jsonl`

1. Confirm the hook server is listening:
   ```bash
   lsof -nP -iTCP:9120 -sTCP:LISTEN | grep node
   ```
2. Confirm the `[[hooks]]` block exists in the correct `cc-connect` config
3. Restart `cc-connect`
4. Check `/tmp/cc-relay-hub-hook.log`
5. Check the relevant `cc-connect` log for hook delivery errors

### `send` reports connection refused

The target project's webhook is down or misconfigured. Check:

- `[webhook] enabled = true`
- `[webhook] port = ...`
- the target `cc-connect` process is running
- `cc-relay-hub info <agent>` shows the expected webhook endpoint

### Hook delivery is slow or missing

This setup assumes `async = false` for `message.sent` hooks. If you changed it to `true`, reply monitoring becomes best-effort and no longer guarantees synchronous hook delivery to the local relay.

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.cc-relay-hub.hook.plist 2>/dev/null || true
rm -f ~/Library/LaunchAgents/com.cc-relay-hub.hook.plist
rm -f ~/bin/cc-relay-hub
rm -rf ~/.cc-connect/cc-relay-hub
```

Remove the `[[hooks]]` block from each `cc-connect` config if you no longer want local reply monitoring.
