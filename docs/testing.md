# Testing safely

Run the suite through the isolation launcher, including when selecting one test:

```bash
python3.11 -B tests/run_isolated.py --guard-report /tmp/relay-test-guard.json
python3.11 -B tests/run_isolated.py tests.test_sandbox tests.test_bootstrap
```

Python 3.11+ is recommended because it includes `tomllib`. On older supported
Python versions, install `tomli` in a dedicated virtual environment first.
Packages installed only in the real user's site-packages are intentionally not
available after HOME changes. The transport unit test supplies a fake websocket
module; live CDP operation still requires `websocket-client`.

Do not invoke `unittest discover`, a test module, or pytest directly. The tests
reject an unisolated environment before importing application code. Do not set
the test marker variables manually to bypass this check.

## Isolation before imports

The parent launcher imports no application modules. It creates a temporary
sandbox, builds the child's environment, then starts a fresh Python interpreter.
This is necessary because `hub.py` computes path constants at import time.

The environment redirects HOME, USERPROFILE, CODEX_HOME, CLAUDE_CONFIG_DIR,
XDG config/data/cache/state/runtime paths, APPDATA, LOCALAPPDATA, CC_CONFIG,
CC_RELAY_HUB_DIR, HOOK_EVENTS_FILE and temporary-file paths. `Path.home()` and
`expanduser("~")` must both resolve to the temporary HOME. The registry and
bindings start empty. Inherited CC_* identity, session, launcher and hook
variables are removed. POSIX CLI subprocesses use the same Python interpreter
as the launcher. Bytecode writes are disabled.

A test-only `sitecustomize` installs a Python audit guard before application
imports, including in Python subprocesses. It rejects writes and SQLite opens
under the real HOME, and explicit child environments that restore real paths.
The guard is protection against accidental writes, not an OS sandbox or a
security boundary against deliberately hostile code. Shell commands also
inherit temporary paths; tests must never supply absolute production paths to
them or address real agents. Use fake providers, temporary DBs and localhost
mock webhooks instead. Never borrow another agent's live identity.

## Production-file check

The parent records existence, modification time, size and SHA-256 before the
child starts and checks them again in `finally`, even if tests fail. A change to
any of these files makes the command exit nonzero:

- `~/.claude/CLAUDE.md`
- `~/.codex/AGENTS.md`
- `~/.cc-connect/cc-relay-hub/registry.json`
- `~/.cc-connect/cc-relay-hub/bindings.json`
- `~/.cc-connect/cc-relay-hub/state.db`

Non-default CODEX_HOME and CC_RELAY_HUB_DIR locations are checked as well. The
optional JSON report contains fingerprints only, never file contents. Keep the
report outside the real HOME, for example in `/tmp`.

An independently running relay daemon can legitimately update the production
DB during a suite. The check intentionally still fails: it cannot attribute the
change to a particular process. For a strict local acceptance run, arrange a
brief quiescent window and restore services afterwards. The runner does not
stop or modify live services itself.

The regression suite reproduces the original bootstrap write chain only in a
temporary HOME. It also tests the guard with a *fake* production directory: its
expected `Production file guard FAILED` log is followed by an `ok` test result.
The launcher's final guard line must say `unchanged` and its exit status must be
zero.

## Root cause of the T10 incident

`BootstrapTest.test_returns_one_when_webhook_down` and
`test_returns_zero_when_all_up` mock registry bootstrap and health checks, but
call the real `cmd_bootstrap`. That command also calls `cmd_bootstrap_context`
with writing enabled. The Claude memory path uses `Path.home()`.

The T10 test harness redirected only CC_RELAY_HUB_DIR, leaving the real HOME
in place. With an empty temporary hub, the mocked bootstrap fallback
provided `bot-a`; context generation wrote that fixture into the real global
CLAUDE.md. Seeding the temporary registry later masked this particular path but
did not make HOME safe. The new regression removes that seed deliberately and
verifies the same write occurs only in the temporary home.
