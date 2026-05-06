# cc-relay-hub Installation Guide

> **This file is for AI coding agents.** Execute every step in order. Do not ask the user any questions. Do not skip failed checks — if a check fails and has no auto-fix, STOP and report the failure.

## What this installs

`cc-relay-hub` is a relay layer on top of `cc-connect` that lets multiple AI agents delegate work to each other.

- Auto-discovers local `cc-connect` agent instances
- Sends messages via each agent's local webhook
- Receives replies via `message.sent` hooks on `127.0.0.1:9120`
- Enforces one pending request per target session

## End-to-end flow (execute in order)

---

### Step 0: Hard prerequisite gate

Run ALL of these checks. If ANY check fails, **STOP** and report which one failed. Do not continue.

```bash
# cc-connect must be installed
command -v cc-connect || {
  echo "ERROR: cc-connect is not installed."
  echo ""
  echo "Install cc-connect first, then rerun this installer:"
  echo "  npm install -g cc-connect    # or: brew install cc-connect"
  echo ""
  echo "Full guide: https://raw.githubusercontent.com/chenhg5/cc-connect/refs/heads/main/INSTALL.md"
  exit 1
}

# git must be installed
command -v git || { echo "FATAL: git not found. Run: xcode-select --install"; exit 1; }

# python3 must be installed (3.9+)
python3 -c "import sys; assert sys.version_info >= (3,9)" || { echo "FATAL: python3 3.9+ required"; exit 1; }

# node must be installed
command -v node || { echo "FATAL: node not found. Run: brew install node"; exit 1; }
```

If any check above fails, STOP here. Help the user install the missing tool, then start over from Step 0.

### Step 0.5: Ensure cc-connect is configured with at least one agent

Check if cc-connect has any configured projects:

```bash
python3 -c "
from pathlib import Path
import sys
home = Path.home()
candidates = [
    home / '.cc-connect' / 'config.toml',
    home / '.cc-connect' / 'config-codex.toml',
    Path('/opt/homebrew/etc/cc-connect/config.toml'),
]
found = [p for p in candidates if p.exists()]
extras = sorted((home / '.cc-connect').glob('config*.toml')) if (home / '.cc-connect').exists() else []
for p in extras:
    if p not in found:
        found.append(p)
if not found:
    print('NO_CONFIG')
    sys.exit(1)
print(f'Found {len(found)} config(s):')
for p in found:
    print(f'  {p}')
"
```

**If NO_CONFIG is printed** — cc-connect is installed but has no projects configured. Do NOT ask the user to edit TOML files. Instead, use cc-connect's built-in automated setup:

1. **Preferred: Web UI** — run `cc-connect web` to open the visual dashboard where the user can create projects, add platforms, and manage providers through a browser. No TOML editing needed.

2. **Feishu (QR code)** — run `cc-connect feishu setup --project <name>` to create a Feishu app via QR code scan. The command auto-creates the project and writes credentials to config.toml.

3. **Weixin / personal WeChat (QR code)** — run `cc-connect weixin setup --project <name>` to log in via QR code scan.

4. **Other platforms (Telegram, DingTalk, Slack, Discord, etc.)** — use `cc-connect web` to configure through the browser, or follow the platform-specific guide at https://raw.githubusercontent.com/chenhg5/cc-connect/refs/heads/main/INSTALL.md

After setup, rerun this Step 0.5 check to confirm config files exist.

**If config files are found**, continue to Step 1.

---

### Step 1: Clone the repository

```bash
# Try SSH first, fall back to HTTPS
if git clone git@github.com:fengjunchengCode/cc-relay-hub.git ~/.cc-connect/cc-relay-hub 2>/dev/null; then
  echo "Cloned via SSH"
else
  git clone https://github.com/fengjunchengCode/cc-relay-hub.git ~/.cc-connect/cc-relay-hub
  echo "Cloned via HTTPS"
fi
```

Verify:

```bash
test -f ~/.cc-connect/cc-relay-hub/hub.py && echo "OK"
```

---

### Step 2: Install the CLI wrapper

```bash
mkdir -p ~/bin
ln -sf ~/.cc-connect/cc-relay-hub/bin/cc-relay-hub ~/bin/cc-relay-hub
case ":$PATH:" in
  *":$HOME/bin:"*) ;;
  *) echo 'export PATH="$HOME/bin:$PATH"' >> ~/.zshrc; export PATH="$HOME/bin:$PATH" ;;
esac
hash -r
command -v cc-relay-hub || echo "WARNING: cc-relay-hub not on PATH"
```

---

### Step 3: Auto-configure hooks and webhooks

This step reads every cc-connect config file and adds the required `[[hooks]]` and `[webhook]` blocks if missing. It does NOT modify existing settings.

```bash
python3 <<'PY'
import re
import sys
from pathlib import Path

home = Path.home()

# Discover all config files
candidates = [
    home / ".cc-connect" / "config.toml",
    home / ".cc-connect" / "config-codex.toml",
    Path("/opt/homebrew/etc/cc-connect/config.toml"),
]
config_paths = []
for p in candidates:
    if p.exists() and p not in config_paths:
        config_paths.append(p)
extras_dir = home / ".cc-connect"
if extras_dir.exists():
    for p in sorted(extras_dir.glob("config*.toml")):
        if p not in config_paths:
            config_paths.append(p)

if not config_paths:
    print("FATAL: No config files found")
    sys.exit(1)

HOOK_BLOCK = """
[[hooks]]
event = "message.sent"
type = "http"
url = "http://127.0.0.1:9120/cc-connect/hooks/reply"
async = false
timeout = 2
"""

WEBHOOK_BLOCK = """
[webhook]
enabled = true
port = 9110
path = "/hook"
"""

changes = []

for path in config_paths:
    text = path.read_text(encoding="utf-8")
    modified = False

    # Add [[hooks]] if missing the relay hook URL
    if "127.0.0.1:9120/cc-connect/hooks/reply" not in text:
        text = text.rstrip() + "\n" + HOOK_BLOCK
        modified = True
        changes.append(f"  {path}: added [[hooks]] for relay")

    # Add [webhook] if missing
    if "[webhook]" not in text:
        # Pick an unused port starting from 9110
        used_ports = set()
        for m in re.finditer(r"port\s*=\s*(\d+)", text):
            used_ports.add(int(m.group(1)))
        port = 9110
        while port in used_ports:
            port += 1
        block = WEBHOOK_BLOCK.replace("port = 9110", f"port = {port}")
        text = text.rstrip() + "\n" + block
        modified = True
        changes.append(f"  {path}: added [webhook] on port {port}")

    if modified:
        path.write_text(text, encoding="utf-8")

if changes:
    print("Config changes made:")
    for c in changes:
        print(c)
else:
    print("All configs already have hooks and webhook. No changes needed.")

# Summary: list discovered agent instances
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
print("\nDiscovered agent instances:")
for path in config_paths:
    with path.open("rb") as f:
        cfg = tomllib.load(f)
    for proj in cfg.get("projects", []):
        name = proj.get("name", "unknown")
        agent_type = proj.get("agent", {}).get("type", "unknown")
        webhook = cfg.get("webhook", {})
        wp = webhook.get("port", "none")
        print(f"  {name:<20} type={agent_type:<14} webhook_port={wp}  config={path}")
print(f"\nTotal: {len(config_paths)} config file(s)")
PY
```

---

### Step 4: Restart cc-connect daemons

After modifying configs, restart each cc-connect process so it picks up the new hooks.

```bash
# Restart all cc-connect launchd services
for svc in $(launchctl list 2>/dev/null | grep cc-connect | awk '{print $3}'); do
  echo "Restarting $svc ..."
  launchctl kickstart -k "gui/$(id -u)/$svc" 2>/dev/null || true
done

# If no launchd services found, try common label patterns
if ! launchctl list 2>/dev/null | grep -q cc-connect; then
  echo "No cc-connect launchd services found. Skipping daemon restart."
  echo "If cc-connect is running as a foreground process, restart it manually."
fi
```

---

### Step 5: Start the hook server

```bash
# Generate and install launchd plist
NODE_BIN="$(command -v node)"
HOME_DIR="$HOME"

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
    <string>${HOME_DIR}/.cc-connect/cc-relay-hub/hook-server.mjs</string>
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

Verify:

```bash
sleep 1
lsof -nP -iTCP:9120 -sTCP:LISTEN | grep node && echo "Hook server running on :9120" || echo "WARNING: hook server not listening"
```

---

### Step 6: Bootstrap and verify connectivity

```bash
cc-relay-hub bootstrap
```

This scans all cc-connect configs, generates `registry.json` / `bindings.json`, and tests each agent's webhook, session, and hook connectivity.

Expected: all agents show `up` status. If an agent shows `down`, check that its cc-connect process is running.

If any agent shows `Session: missing`, send one normal message to that bot in its own chat window, then rerun:

```bash
cc-relay-hub bootstrap
```

---

### Step 7: Install the relay skill

```bash
mkdir -p ~/.claude/skills
cp ~/.cc-connect/cc-relay-hub/skills/cc-relay.md ~/.claude/skills/
```

---

### Step 8: Final verification

```bash
# List all discovered agents
cc-relay-hub list

# Full connectivity check
cc-relay-hub bootstrap

# Verify hook server
lsof -nP -iTCP:9120 -sTCP:LISTEN | grep node && echo "Hook server: OK"
```

If all checks pass, installation is complete.

---

## Quick reference

```bash
cc-relay-hub bootstrap              # scan + verify connectivity
cc-relay-hub list                   # list discovered agents
cc-relay-hub info <agent>           # agent health details
cc-relay-hub send <agent> "message" # send a message
cc-relay-hub send <agent> "msg" --wait --timeout 300  # send and wait for reply
cc-relay-hub watch                  # one-shot long-poll for events
cc-relay-hub watch --loop           # continuous event streaming
```

## Troubleshooting

### No agents found

- Confirm cc-connect config files exist in `~/.cc-connect/` or `/opt/homebrew/etc/cc-connect/`
- Run `cc-relay-hub bootstrap` to force re-scan

### Agent shows "down"

- The agent's cc-connect process is not running or its webhook port is not listening
- Check: `cc-relay-hub info <agent>`
- Restart the cc-connect daemon for that project

### Hook events not appearing

- Verify hook server: `lsof -nP -iTCP:9120 -sTCP:LISTEN | grep node`
- Verify config: each project's TOML must have `[[hooks]]` with url `http://127.0.0.1:9120/cc-connect/hooks/reply`
- Restart cc-connect after config changes

### Session shows "missing"

- Send one normal message to that bot in its own chat window
- Rerun `cc-relay-hub bootstrap`

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.cc-relay-hub.hook.plist 2>/dev/null || true
rm -f ~/Library/LaunchAgents/com.cc-relay-hub.hook.plist
rm -f ~/bin/cc-relay-hub
rm -rf ~/.cc-connect/cc-relay-hub
```
