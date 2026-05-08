# Providers

cc-relay-hub currently supports two provider families: `cc_connect` and `cdp`.

| Provider | Transport | Reply detection | Best fit |
| --- | --- | --- | --- |
| `cc_connect` | Local HTTP webhook | `message.sent` hook server | Claude Code, Codex, Gemini CLI, or any agent already bridged by cc-connect |
| `cdp` | Chrome DevTools Protocol WebSocket | DOM transcript polling with relay markers | Electron IDEs such as Antigravity, Cursor, or other IDE UIs with local CDP enabled |

## cc-connect Provider

The cc-connect provider sends a message to a local cc-connect webhook. Replies come back through the relay hook server.

Example registry entry:

```json
{
  "version": 2,
  "agents": {
    "codex-bot": {
      "type": "codex",
      "provider": "cc_connect",
      "work_dir": "/path/to/workdir",
      "capabilities": ["message.send", "history.read", "session.control"],
      "labels": []
    }
  }
}
```

Example binding:

```json
{
  "cc_connect": {
    "codex-bot": {
      "config_path": "/path/to/.cc-connect/config-codex.toml",
      "webhook_host": "127.0.0.1",
      "webhook_port": 9112,
      "webhook_path": "/hook",
      "session_key": "feishu:chat_id:user_id"
    }
  }
}
```

## CDP Provider

The CDP provider controls an Electron IDE through Chrome DevTools Protocol. It can type a relay prompt into the IDE, wait for the answer, and extract the relay marker from the visible transcript.

Example registry entry:

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

Example binding:

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

## CDP Safety

CDP can control the target IDE window. Keep debugging ports bound to `127.0.0.1`, do not expose them to a network, and close the IDE debugging session when not needed.

## Backend Status

| Backend | Status | Notes |
| --- | :---: | --- |
| Antigravity | ✅ | Primary CDP backend |
| Cursor | ⚠️ | Stub/backend work in progress |
| Other Electron IDEs | ⚠️ | Requires a backend that can locate the chat input and transcript |
