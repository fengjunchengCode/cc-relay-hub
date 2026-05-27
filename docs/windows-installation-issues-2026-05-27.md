# Windows installation incident analysis - 2026-05-27

This document summarizes a real Windows installation of `cc-relay-hub` on
2026-05-27. It combines observations from the live install, a local analysis
note on the user's desktop, and a second analysis produced by the existing
`my-project` Claude Code agent.

No local app secrets, API keys, tokens, or full session identifiers are included
here. Values observed during the install are intentionally redacted.

## Executive summary

The installation failures were mixed:

| Category | Count | Summary |
| --- | ---: | --- |
| `cc-relay-hub` bugs or documentation gaps | 6 | Windows path handling, Python discovery, copied `.cmd` wrapper, startup script working directory, `cc-connect` subprocess resolution, and incomplete Windows install guidance. |
| Already mitigated on current `main` | 1 | The shell wrapper now falls back from broken `python3` to `python`; this was still relevant during analysis of older install artifacts. |
| External environment or upstream `cc-connect`/Codex issues | 3 | GitHub proxy requirement, Codex WindowsApps executable chosen by `cc-connect`, and `cc-connect feishu setup --config <new-file>` not creating a config from nothing. |
| Operator/process issues during this run | 2 | The Codex cc-connect setup was attempted before completing the Feishu onboarding flow, and some verification commands were run concurrently with bootstrap. |

So the answer is: this was not only a workflow problem. Several real
`cc-relay-hub` Windows bugs remain, but the installation was also made noisier
by operator sequencing mistakes and by upstream Windows behavior in
`cc-connect`/Codex.

## Environment

- OS: Windows 11 Pro
- Shells used: PowerShell and Git Bash style commands
- Python: 3.13.1 available as `python`
- Node.js: available
- `cc-connect`: 1.3.2
- Existing agent before install: `my-project` / `claudecode`
- New agent added during install: `codex-current` / `codex`
- `cc-relay-hub` remote checked during final report: `main` at `7483805`

## Detailed findings

### P0: `hook-server.mjs` builds invalid Windows paths

**Classification:** `cc-relay-hub` bug.

**Evidence:** The hook server log contained paths shaped like:

```text
D:\C:\Users\...\cc-relay-hub\hook-events.jsonl
```

The current code still derives `hubDir` with:

```js
const hubDir = path.dirname(new URL(import.meta.url).pathname);
```

On Windows, `new URL(import.meta.url).pathname` can return `/C:/Users/...`.
When the current drive is `D:`, that pseudo-absolute path can be resolved as
`D:\C:\Users\...`.

**Impact:** The hook server can fail to persist hook events and can compute an
invalid path for `hub.py`.

**Recommended fix:** Use Node's URL-safe filesystem conversion:

```js
import { fileURLToPath } from "node:url";

const hubDir = path.dirname(fileURLToPath(import.meta.url));
```

**Tests to add:**

- A unit-level Node test or small subprocess test proving `hubDir` resolves to a
  drive-qualified path on Windows.
- A Windows CI test that starts `hook-server.mjs` from a different drive/current
  working directory and posts a hook event successfully.

### P0: `hook-server.mjs` hardcodes `python3`

**Classification:** `cc-relay-hub` bug.

**Evidence:** `hook-server.mjs` forwards events with:

```js
spawn("python3", [hubScript, "_on_hook"], ...)
```

On Windows, `python3` often resolves to the Microsoft Store app execution alias
instead of a real Python installation. In this installation, the real usable
interpreter was `python`.

**Impact:** The hook server can receive cc-connect events but fail to invoke
`hub.py _on_hook`, which prevents reply matching and origin notification.

**Recommended fix:**

- Reuse the Python discovery behavior from `INSTALL.md` and wrappers.
- Prefer `process.env.PYTHON_BIN` when set.
- On Windows, try `python`, then `py -3`, then `python3`.
- On Unix, try `python3`, then `python`.

**Tests to add:**

- A hook server subprocess test with a fake `python3` that fails and a fake
  `python` that records invocation.
- A Windows CI case that posts a relay reply and verifies `_on_hook` is called.

### P1: copied `bin/cc-relay-hub.cmd` points at the wrong `hub.py`

**Classification:** `cc-relay-hub` bug and install documentation gap.

**Evidence:** `INSTALL.md` tells Windows users to copy the `.cmd` wrapper:

```powershell
Copy-Item "$HOME\.cc-connect\cc-relay-hub\bin\cc-relay-hub.cmd" "$Bin\cc-relay-hub.cmd" -Force
```

But `bin/cc-relay-hub.cmd` computes:

```bat
set "SCRIPT_DIR=%~dp0"
set "HUB=%SCRIPT_DIR%..\hub.py"
```

After copying to `C:\Users\<user>\bin`, `..\hub.py` resolves to
`C:\Users\<user>\hub.py`, not `C:\Users\<user>\.cc-connect\cc-relay-hub\hub.py`.

**Impact:** A documented Windows installation can create a `cc-relay-hub` command
that immediately fails with `hub.py` not found.

**Recommended fix options:**

1. Prefer a Windows symlink or junction instead of copying.
2. Generate a small user-local wrapper during install that embeds the absolute
   install path.
3. Teach `cc-relay-hub.cmd` to fall back to
   `%USERPROFILE%\.cc-connect\cc-relay-hub\hub.py` when `..\hub.py` does not
   exist.

**Tests to add:**

- A Windows wrapper test that copies `cc-relay-hub.cmd` to a temporary `bin`
  directory and verifies it can still run `list`.
- A test for a moved wrapper without relying on the source tree layout.

### P1: relay origin notification cannot always find `cc-connect` on Windows

**Classification:** `cc-relay-hub` bug.

**Evidence:** A successful relay from `codex-current` to `my-project` received
the expected Claude reply, but `cc-relay-hub relay` then crashed during origin
notification:

```text
FileNotFoundError: [WinError 2] The system cannot find the file specified
```

The failing command was built as:

```python
["cc-connect", "--config", config_path, "send", ...]
```

On Windows, npm commonly exposes `cc-connect.cmd` and an extensionless shim, but
Python `subprocess.run(["cc-connect", ...])` does not reliably resolve those in
all environments. Creating `C:\Users\<user>\bin\cc-connect.exe` made the same
path work locally.

**Impact:** `relay` can deliver the target request and observe the target reply,
yet exit non-zero while attempting to notify the origin session.

**Recommended fix:**

- Add a resolver for local executables used by Python subprocesses.
- On Windows, prefer `cc-connect.exe`, then `cc-connect.cmd`, then `cc-connect`.
- Use `shutil.which` and pass the resolved path to `subprocess.run`.
- Consider falling back to the existing webhook notification path when config
  notification fails with `FileNotFoundError`.

**Tests to add:**

- A Windows-focused unit test for `_send_via_origin_config` where only
  `cc-connect.cmd` exists on `PATH`.
- A regression test that `cmd_relay` does not crash when config notification
  cannot resolve `cc-connect`; it should return a structured failure or use the
  webhook fallback.

### P2: Windows startup script does not set the hook server working directory

**Classification:** `cc-relay-hub` documentation gap, made more severe by the
`hook-server.mjs` path bug.

**Evidence:** The Windows startup block in `INSTALL.md` writes:

```bat
"$Node" "$Root\hook-server.mjs" >> "%TEMP%\cc-relay-hub-hook.log" 2>&1
```

It does not `cd /d` into the hub directory before launching.

**Impact:** Once `hook-server.mjs` uses robust absolute paths this should matter
less, but startup scripts should still avoid CWD-dependent behavior.

**Recommended fix:**

```bat
@echo off
cd /d "%USERPROFILE%\.cc-connect\cc-relay-hub"
"<node>" "%USERPROFILE%\.cc-connect\cc-relay-hub\hook-server.mjs" >> "%TEMP%\cc-relay-hub-hook.log" 2>&1
```

**Tests to add:**

- A documentation asset test that the generated Windows startup script includes
  `cd /d`.

### P2: Windows Codex onboarding needs explicit PATH validation

**Classification:** External `cc-connect`/Codex/Windows issue, but
`cc-relay-hub` install docs should guard against it.

**Evidence:** After the new Feishu bot was created and a first `hi` message was
sent, cc-connect failed to start Codex:

```text
codexSession: start: fork/exec C:\Program Files\WindowsApps\OpenAI.Codex_...\resources\codex.exe: Access is denied.
```

The working Codex CLI was actually under:

```text
C:\Users\<user>\AppData\Local\OpenAI\Codex\bin\...\codex.exe
```

A user-local `codex.cmd` shim placed before WindowsApps on `PATH` fixed the
problem.

**Impact:** Codex-backed cc-connect agents can appear configured but fail on the
first real message.

**Recommended docs change:**

- Before configuring a Codex cc-connect project on Windows, run:

```powershell
where.exe codex
codex --version
```

- If the first result is under `C:\Program Files\WindowsApps`, create or install
  a shim that points to the real Codex CLI, and put that shim directory before
  WindowsApps in user `PATH`.

**Ownership:** This is probably upstream `cc-connect`/Codex PATH behavior, not a
core `cc-relay-hub` bug. It is still worth documenting because relay-hub install
flows often create or validate Codex peers.

### P2: `cc-connect feishu setup --config <new-file>` does not create a config

**Classification:** External `cc-connect` behavior plus operator/process issue.

**Evidence:** Running Feishu setup against a custom config path that did not yet
exist produced:

```text
prepare project failed: read config: open ...\config-codex.toml: The system cannot find the file specified.
```

Creating a minimal skeleton `config-codex.toml` first, then running
`cc-connect feishu bind`, worked.

**Impact:** An agent attempting to add a second cc-connect instance in a separate
config file can accidentally send the user through a QR onboarding flow that
cannot write credentials back to disk.

**Recommended docs change:**

- If using the default config, `cc-connect feishu setup --project <name>` is OK.
- If using a separate config path such as `~/.cc-connect/config-codex.toml`,
  create a minimal project skeleton first, or use `cc-connect feishu bind` after
  the app is created.
- The install guide should explicitly pause after QR creation and wait for the
  config file to be updated before continuing to relay bootstrap.

### P2: proxy requirement for GitHub access

**Classification:** Environment issue.

**Evidence:** GitHub access required a local proxy in this Windows environment.

**Impact:** `git clone` or `git fetch` can fail even though the installer is
otherwise correct.

**Recommended docs change:**

- Add an optional troubleshooting note for users behind a local proxy:

```bash
git -c http.proxy=http://127.0.0.1:<port> \
    -c https.proxy=http://127.0.0.1:<port> \
    clone https://github.com/fengjunchengCode/cc-relay-hub.git ~/.cc-connect/cc-relay-hub
```

This is not a project bug.

### Already mitigated: shell wrapper fallback from `python3` to `python`

**Classification:** Was a Windows compatibility bug in older artifacts; current
`main` has a mitigation.

**Evidence:** The current `bin/cc-relay-hub` uses `run_if_usable` and the test
suite includes `test_shell_wrapper_falls_back_to_python_when_python3_fails`.

**Residual risk:** The `.cmd` wrapper still tries `python3` first, but its checks
are guarded and should fall through to `python` or `py -3` if `python3` is not
usable. The moved-wrapper path issue remains separate and more important.

## Process mistakes observed in this run

The install flow also had two operator sequencing mistakes:

1. The Codex peer was temporarily configured before the separate Feishu app
   onboarding was complete. This reused an existing Feishu app during an early
   experiment and had to be rolled back.
2. Some verification commands were run in parallel with `bootstrap`, causing a
   transient `list` output that showed only one agent while the registry was
   still being refreshed.

These are not repository bugs by themselves, but the install guide can reduce
the chance of them by adding explicit "pause and verify" checkpoints:

- After QR setup: verify the target config contains the new platform credentials.
- After starting each cc-connect instance: verify its webhook port is listening.
- After first user message to the bot: verify a session file exists.
- Only then run `cc-relay-hub bootstrap` and `cc-relay-hub list`.

## Remediation plan

### Phase 1: fix core Windows breakages

1. Replace `new URL(import.meta.url).pathname` with `fileURLToPath` in
   `hook-server.mjs`.
2. Add shared Python command discovery for `hook-server.mjs`; honor `PYTHON_BIN`.
3. Fix the Windows `.cmd` installation path problem by using a generated wrapper,
   symlink/junction, or fallback install path.
4. Resolve `cc-connect` before Python subprocess calls and support `.cmd`/`.exe`
   shims on Windows.

### Phase 2: harden the installer documentation

1. Update the Windows PowerShell startup script to set `cd /d` explicitly.
2. Add Codex-on-Windows PATH validation and a documented shim workaround.
3. Add a separate-config Feishu onboarding section that explains when to use
   `setup` versus `bind`.
4. Add a short proxy troubleshooting section.
5. Add a checklist that prevents moving to relay bootstrap before platform
   credentials, webhook port, and session file are all present.

### Phase 3: add regression coverage

1. Add Windows CI for the existing test suite.
2. Add tests for `hook-server.mjs` path resolution and Python discovery.
3. Add tests for copied `.cmd` wrapper behavior.
4. Add tests for `cc-connect` command resolution in `_send_via_origin_config`.
5. Add an end-to-end Windows smoke test that:
   - boots the hook server,
   - writes two fake cc-connect bindings,
   - posts a relay reply hook,
   - verifies reply matching and origin notification behavior.

## Suggested ownership

| Item | Owner |
| --- | --- |
| `hook-server.mjs` path and Python fixes | `cc-relay-hub` |
| `.cmd` copied-wrapper behavior | `cc-relay-hub` |
| `cc-connect` executable resolution in Python subprocesses | `cc-relay-hub` |
| Windows startup script and install checklist | `cc-relay-hub` docs |
| Codex WindowsApps executable selection | upstream `cc-connect` or Codex, with `cc-relay-hub` docs workaround |
| `feishu setup --config <new-file>` behavior | upstream `cc-connect`, with `cc-relay-hub` docs workaround |
| GitHub proxy requirement | user environment, with docs troubleshooting note |

## Final classification

The installation issues were mostly project-side Windows hardening gaps, not
just user workflow mistakes. The clean split is:

- **Current `cc-relay-hub` bugs/gaps to fix:** hook server path resolution,
  hook server Python discovery, moved `.cmd` wrapper, Windows startup CWD,
  Windows `cc-connect` subprocess resolution, and missing installer checkpoints.
- **External/upstream issues to document:** Codex WindowsApps PATH selection,
  `cc-connect feishu setup --config` behavior, and local network proxy needs.
- **Operator mistakes from this run:** proceeding past Feishu onboarding too
  early and running parallel verification during bootstrap.

Fixing the Phase 1 items should make the next Windows install much closer to a
straight-through path. The Phase 2 docs changes should prevent agents from
repeating the sequencing mistakes that happened during this installation.
