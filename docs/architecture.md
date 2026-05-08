# cc-relay-hub Architecture

This document keeps the implementation details out of the product README. It explains how messages move through cc-relay-hub, how replies are matched, and where the cc-connect and CDP paths differ.

## High-level Flow

```text
Chat App
   |
   v
cc-connect project A  -- cc-relay-hub send -->  cc-connect project B
   ^                                                |
   |                                                v
   +------- hook server receives message.sent <-----+
            and forwards reply to project A
```

1. Agent A calls `cc-relay-hub send <agent> "<task>"`.
2. cc-relay-hub discovers Agent B from the local registry and binding files.
3. The task is delivered through Agent B's local cc-connect webhook.
4. Agent B replies; cc-connect emits a `message.sent` hook.
5. cc-relay-hub matches the reply to the original request and sends it back to Agent A's session.

## CDP Flow

CDP agents use the same relay core with a different provider path:

```text
Agent / Skill  -- cc-relay-hub send -->  CDP provider  -- WebSocket -->  Electron IDE
     ^                                      |
     +--------- marker-matched reply <------+
```

The CDP provider types the relay prompt into the IDE, reads the DOM transcript, and extracts the `[cc-relay reply_to=...]` marker from the answer. CDP replies do not require the hook server.

## Internal Components

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

- **Discovery** reads local `~/.cc-connect/config*.toml` files and session metadata.
- **Providers** send through cc-connect local webhooks or local CDP WebSockets.
- **State** stores request IDs, session locks, delivery status, replies, and notification status.
- **Hook server** receives `message.sent` events and forwards matched replies to the origin session.
- **Context generation** writes project-local instructions so agents can discover peers and follow the relay protocol.

## Reply Markers

Relay requests include a request marker and a reply marker instruction. The target agent is asked to start the final answer with:

```text
[cc-relay reply_to=<request_id>]
```

The relay core uses this marker to associate a reply with the original request. This is especially important for CDP transcripts, where the provider reads text from the IDE DOM rather than receiving a hook event.

## Group-aware Resolution

Agent lookup follows this order:

1. Exact agent name.
2. Fuzzy match by name substring or agent type.
3. Optional `--group` narrowing.
4. Sender's group preference.
5. Disambiguation error if multiple candidates remain.

The goal is to make commands such as `send codex` useful when a project has multiple Codex-like agents, while still requiring exact names when the target is ambiguous.
