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

# Python 3.9+ must be installed. Unix usually has python3; Windows/Git Bash often has python.
if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN=python3
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN=python
else
  echo "FATAL: Python 3.9+ not found. Install Python and rerun this installer."
  exit 1
fi
"$PYTHON_BIN" - <<'PY' || { echo "FATAL: Python 3.9+ with tomllib or tomli required. For Python 3.9/3.10, run: $PYTHON_BIN -m pip install --user tomli"; exit 1; }
import sys
assert sys.version_info >= (3, 9)
try:
    import tomllib
except ModuleNotFoundError:
    import tomli
PY
export PYTHON_BIN

# node must be installed
command -v node || { echo "FATAL: node not found. Run: brew install node"; exit 1; }
```

If any check above fails, STOP here. Help the user install the missing tool, then start over from Step 0.

### Step 0.5: Ensure cc-connect is configured with at least one agent

Check if cc-connect has any configured projects:

```bash
${PYTHON_BIN:-python3} -c "
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

If you are adding a second Feishu bot to a separate config file such as `~/.cc-connect/config-codex.toml`, do not run setup against a missing file. Create a minimal project skeleton first, then use `cc-connect feishu bind --config <path>` after the Feishu app exists. Pause here until the target config contains platform credentials.

**If config files are found**, continue to Step 0.6.

---

### Step 0.6: Validate work_dir in each config

cc-connect's default config may contain `work_dir = "/path/to/your/project"` — a placeholder that prevents the agent from starting. Check and fix automatically:

```bash
${PYTHON_BIN:-python3} <<'PY'
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
import sys
from pathlib import Path

home = Path.home()
config_paths = [
    home / ".cc-connect" / "config.toml",
    home / ".cc-connect" / "config-codex.toml",
    Path("/opt/homebrew/etc/cc-connect/config.toml"),
]
extras = sorted((home / ".cc-connect").glob("config*.toml")) if (home / ".cc-connect").exists() else []
for p in extras:
    if p not in config_paths:
        config_paths.append(p)

needs_fix = []
for path in config_paths:
    if not path.exists():
        continue
    with path.open("rb") as f:
        cfg = tomllib.load(f)
    for proj in cfg.get("projects", []):
        name = proj.get("name", "unknown")
        wd = proj.get("agent", {}).get("options", {}).get("work_dir", "")
        if not wd or not Path(wd).exists() or "path/to" in wd:
            needs_fix.append((path, name, wd))

if needs_fix:
    print("Invalid work_dir found:")
    for path, name, wd in needs_fix:
        print(f"  {path} project={name} work_dir={wd}")
    print(f"\nFix: replace work_dir with your actual project directory in each config file.")
    print(f"  Example: work_dir = \"{home}\"")
    sys.exit(1)
else:
    print("All work_dir paths are valid.")
PY
```

If this check fails, fix the `work_dir` in each reported config file, then rerun.

---

### Step 0.7: Windows Codex PATH guard

On Windows, verify the Codex executable before configuring a Codex-backed cc-connect project:

```powershell
where.exe codex
codex --version
```

If the first `where.exe codex` result is under `C:\Program Files\WindowsApps`, put a user-local `codex.cmd` shim before WindowsApps on `PATH` that points to the real Codex CLI under `%LOCALAPPDATA%\OpenAI\Codex\bin\...`. Then rerun `where.exe codex` and `codex --version` before continuing.

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

If GitHub access requires a local proxy, use the same clone command with proxy settings:

```bash
git -c http.proxy=http://127.0.0.1:<port> \
    -c https.proxy=http://127.0.0.1:<port> \
    clone https://github.com/fengjunchengCode/cc-relay-hub.git ~/.cc-connect/cc-relay-hub
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

On Windows PowerShell, install the `.cmd` wrapper:

```powershell
$Bin = Join-Path $HOME "bin"
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
Copy-Item "$HOME\.cc-connect\cc-relay-hub\bin\cc-relay-hub.cmd" "$Bin\cc-relay-hub.cmd" -Force
if (($env:Path -split ';') -notcontains $Bin) {
  $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
  if ([string]::IsNullOrWhiteSpace($UserPath)) {
    [Environment]::SetEnvironmentVariable("Path", $Bin, "User")
  } else {
    [Environment]::SetEnvironmentVariable("Path", "$UserPath;$Bin", "User")
  }
  $env:Path = "$env:Path;$Bin"
  Write-Host "Added $Bin to the user PATH for future terminals."
}
```

---

### Step 3: Auto-configure hooks and webhooks

This step reads every cc-connect config file and adds the required `[[hooks]]` and `[webhook]` blocks if missing. It does NOT modify existing settings.

```bash
${PYTHON_BIN:-python3} <<'PY'
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

### Step 4: Reload cc-connect configs

After modifying configs, the running cc-connect process must reload `config.toml` so it picks up the new `[webhook]` and `[[hooks]]` blocks.

Prefer config reload over process restart. If reload is unavailable, restart the cc-connect daemon. Restarting cc-connect can interrupt this agent conversation because cc-connect may be the transport carrying the reply.

Before any restart fallback, print this message to the user exactly:

```text
cc-connect needs to reload the new hook/webhook config. I will try a daemon restart now. This conversation may disconnect for 10-30 seconds. If I do not resume automatically, wait 30 seconds and send "继续" so I can verify and continue the installation.
```

Do NOT improvise platform commands. Do NOT use `taskkill` / `kill` on a foreground cc-connect process unless the user explicitly asks for a hard reset. Use the matching block for the current shell only.

#### macOS/Linux bash/zsh

```bash
echo 'cc-connect needs to reload the new hook/webhook config. I will try a daemon restart now. This conversation may disconnect for 10-30 seconds. If I do not resume automatically, wait 30 seconds and send "继续" so I can verify and continue the installation.'

# First try cc-connect's service manager. This is safer than killing processes.
if cc-connect daemon status >/tmp/cc-connect-daemon-status.txt 2>&1; then
  cc-connect daemon restart || cc-connect daemon start
else
  # macOS fallback for launchd installs created by older cc-connect versions.
  restarted=0
  if command -v launchctl >/dev/null 2>&1; then
    for svc in $(launchctl list 2>/dev/null | grep cc-connect | awk '{print $3}'); do
      echo "Restarting launchd service $svc ..."
      launchctl kickstart -k "gui/$(id -u)/$svc" 2>/dev/null && restarted=1
    done
  fi

  if [ "$restarted" != "1" ]; then
    echo "WARNING: No cc-connect daemon/service was found."
    echo "Config files were updated, but a foreground cc-connect process cannot be safely restarted from this installer."
    echo "Ask the user to restart cc-connect, then have them send \"继续\" and rerun: cc-relay-hub bootstrap"
  fi
fi
```

#### Windows PowerShell

```powershell
Write-Host 'cc-connect needs to reload the new hook/webhook config. I will restart the current cc-connect process now. This conversation may disconnect for 10-30 seconds. If I do not resume automatically, wait 30 seconds and send "继续" so I can verify and continue the installation.'

$procs = @(Get-CimInstance Win32_Process | Where-Object {
  $_.Name -ieq "cc-connect.exe" -or $_.CommandLine -match "(^|[\\/])cc-connect(\.exe)?(\s|$)"
})

if ($procs.Count -eq 0) {
  Write-Host "No running cc-connect process found. Config files were updated; the next cc-connect start will load them."
} else {
  $restartScript = Join-Path $env:TEMP "cc-connect-restart.ps1"
  $lines = @(
    "Start-Sleep -Seconds 2"
  )
  foreach ($proc in $procs) {
    if (-not $proc.CommandLine) {
      Write-Host "WARNING: Cannot read command line for PID $($proc.ProcessId); skipping automatic restart for that process."
      continue
    }
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($proc.CommandLine))
    $lines += "`$cmd = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('$encoded'))"
    $lines += 'Start-Process -FilePath "cmd.exe" -ArgumentList ("/c start """" " + $cmd) -WindowStyle Hidden'
  }
  Set-Content -Path $restartScript -Value $lines -Encoding UTF8
  Start-Process -FilePath "powershell" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $restartScript) -WindowStyle Hidden

  foreach ($proc in $procs) {
    Stop-Process -Id $proc.ProcessId -Force
  }
}
```

---

### Step 5: Start the hook server

Use the matching block for the current platform. Do NOT translate a block from another platform.

#### macOS bash/zsh

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

#### Linux bash/zsh

```bash
NODE_BIN="$(command -v node)"
HOME_DIR="$HOME"
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/cc-relay-hub-hook.service <<EOF
[Unit]
Description=cc-relay-hub hook server

[Service]
ExecStart=${NODE_BIN} ${HOME_DIR}/.cc-connect/cc-relay-hub/hook-server.mjs
Restart=always
RestartSec=2
StandardOutput=append:/tmp/cc-relay-hub-hook.log
StandardError=append:/tmp/cc-relay-hub-hook.log

[Install]
WantedBy=default.target
EOF

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload
  systemctl --user enable --now cc-relay-hub-hook.service || {
    echo "WARNING: systemd user service failed; falling back to nohup for this session."
    nohup "$NODE_BIN" "$HOME_DIR/.cc-connect/cc-relay-hub/hook-server.mjs" >/tmp/cc-relay-hub-hook.log 2>&1 &
  }
else
  nohup "$NODE_BIN" "$HOME_DIR/.cc-connect/cc-relay-hub/hook-server.mjs" >/tmp/cc-relay-hub-hook.log 2>&1 &
fi

sleep 1
${PYTHON_BIN:-python3} - <<'PY'
import socket
s = socket.socket()
s.settimeout(2)
try:
    s.connect(("127.0.0.1", 9120))
except OSError as e:
    print(f"WARNING: hook server not listening on :9120: {e}")
    raise SystemExit(1)
else:
    print("Hook server running on :9120")
finally:
    s.close()
PY
```

#### Windows PowerShell

```powershell
$Root = Join-Path $HOME ".cc-connect\cc-relay-hub"
$Node = (Get-Command node).Source
$Startup = [Environment]::GetFolderPath("Startup")
$CmdPath = Join-Path $Startup "cc-relay-hub-hook.cmd"

$Cmd = @"
@echo off
cd /d "$Root"
"$Node" "$Root\hook-server.mjs" >> "%TEMP%\cc-relay-hub-hook.log" 2>&1
"@
Set-Content -Path $CmdPath -Value $Cmd -Encoding ASCII

Start-Process -FilePath $CmdPath -WindowStyle Hidden
Start-Sleep -Seconds 1

$client = New-Object Net.Sockets.TcpClient
try {
  $iar = $client.BeginConnect("127.0.0.1", 9120, $null, $null)
  if (-not $iar.AsyncWaitHandle.WaitOne(2000, $false)) {
    throw "timeout"
  }
  $client.EndConnect($iar)
  Write-Host "Hook server running on :9120"
} catch {
  Write-Host "WARNING: hook server not listening on :9120"
  exit 1
} finally {
  $client.Close()
}
```

---

### Step 6: Bootstrap and verify connectivity

Before running bootstrap, pause and verify:

- The target cc-connect config contains platform credentials.
- Each cc-connect instance has been started.
- For each cc-connect instance, verify its webhook port is listening.
- Send one normal message to each bot and verify a session file exists.
- Do not run parallel verification commands while bootstrap is refreshing the registry.

```bash
cc-relay-hub bootstrap
```

This scans all cc-connect configs, generates `registry.json` / `bindings.json`, and tests each agent's webhook, session, and hook connectivity.

Expected: all agents show `up` status. If an agent shows `down`, check that its cc-connect process is running.

> **Important**: cc-connect creates session files on demand. If any agent shows `Session: missing`, you MUST send one normal message to that bot via its chat platform (Feishu/Telegram/etc.) first. For example, open Feishu and send "hi" to the bot. Then rerun:
>
> ```bash
> cc-relay-hub bootstrap
> ```

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

# Verify hook server on macOS/Linux
lsof -nP -iTCP:9120 -sTCP:LISTEN | grep node && echo "Hook server: OK"
```

On Windows PowerShell, verify the hook server with:

```powershell
$client = New-Object Net.Sockets.TcpClient
try {
  $iar = $client.BeginConnect("127.0.0.1", 9120, $null, $null)
  if (-not $iar.AsyncWaitHandle.WaitOne(2000, $false)) { throw "timeout" }
  $client.EndConnect($iar)
  Write-Host "Hook server: OK"
} finally {
  $client.Close()
}
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
- Reload/restart cc-connect using Step 4, then rerun `cc-relay-hub bootstrap`

### Hook events not appearing

- Verify hook server: `lsof -nP -iTCP:9120 -sTCP:LISTEN | grep node`
- Verify config: each project's TOML must have `[[hooks]]` with url `http://127.0.0.1:9120/cc-connect/hooks/reply`
- Reload cc-connect after config changes. Use daemon restart only if reload is unavailable.
- If the install conversation stopped during restart, wait 30 seconds, send `继续`, then rerun `cc-relay-hub bootstrap`

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
