# Changelog

## 0.2.0 — 2026-10-12 · Cross-node federation

Agents on other computers are now first-class peers.

### Added
- **Node daemon** (`noded.py`, `cc-relay-hub node install|start|stop|restart|status|invite|join|doctor|discover`): one process per computer that discovers peers, keeps a durable per-peer outbox, delivers inbound requests to local agents and ships replies back to the origin node.
- **`name@node` addressing** in `send`, `info` and `list` (`cc-relay-hub send codex-bot@office "task"`). Bare names resolve locally first and fall back to remote nodes; ambiguous names must be fully qualified.
- **Automatic path selection**: LAN direct (UDP beacon + address cards) → WAN direct (any reachable advertised address) → encrypted relay. Re-probed every 60 s.
- **Rendezvous relay** over the ntfy protocol (public `ntfy.sh` by default) and a self-hostable `relay_server.py`.
- **End-to-end encryption and request signing** (`core/crypto.py`, stdlib only) for every envelope that leaves a machine; replay protection with timestamp windows and nonces.
- **Cluster membership** with a single invite string (`node invite` / `node join`); `cluster.secret` and `node.json` are ignored by git.
- `peer list|add|remove`, cross-node entries in `inbox`, remote peers in generated CLAUDE.md / AGENTS.md, `docs/federation.md`, an INSTALL.md section an agent can execute.
- Tests: crypto, envelope protocol, `name@node` resolution, and a two-daemon end-to-end suite with a local relay (217 tests total).

### Changed
- `messages` gains `direction`, `origin_node`, `target_node`, `envelope_id`; new tables `peers`, `remote_agents`, `peer_outbox`, `peer_inbox_seen` (migrated automatically).
- Local delivery is factored into `deliver_to_local_agent`; CLI behaviour for local sends is unchanged.
- Agents without a webhook port now fail with a clear, actionable error instead of a socket error.

### Verified
- macOS node and Windows node on the same LAN: discovery through ntfy.sh, automatic upgrade to LAN direct (3 ms), request + reply round trips in both directions, failure propagation for a misconfigured agent.
