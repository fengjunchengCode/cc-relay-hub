# Cross-node federation (other computers)

cc-relay-hub can route private agent-to-agent messages between computers. Each
computer runs one **node daemon** (`cc-relay-hub node start`), and agents on
other computers are addressed as `name@node`:

```bash
cc-relay-hub list                              # remote agents appear as codex-bot@office
cc-relay-hub send codex-bot@office "task"      # reply comes back to this conversation
cc-relay-hub send codex-bot@office "fyi" --no-reply
cc-relay-hub node status                       # peers, paths, outbox
```

Everything that already works locally (reply markers, busy-session gating,
durable outbox, follow-ups, group isolation) is reused unchanged: the request
is delivered on the target computer by its own daemon, and the reply is
injected on the origin computer by its own daemon.

## Setup: two commands per computer

```bash
# every computer, once (creates node.json, cluster secret, login service)
cc-relay-hub node install

# on a computer already in the cluster
cc-relay-hub node invite          # prints ccrelay://join/...

# on the new computer (an agent can run this when you paste the string in chat)
cc-relay-hub node join "ccrelay://join/..."
```

Nothing else is configured: no IPs, no ports to open, no VPN, no accounts.
Within about a minute both computers list each other's agents.

## How computers find each other and talk

Every node derives three keys from the shared cluster secret: an encryption
key, a request-signing key, and an unguessable rendezvous topic name.

| Priority | Path | How it is found | Typical latency |
| --- | --- | --- | --- |
| 1 | LAN direct HTTP | UDP beacon on port 9131 + address cards | < 50 ms |
| 2 | WAN direct HTTP | an address on the peer's card is reachable (port forward, cloud host, VPN) | < 300 ms |
| 3 | Rendezvous relay | always available: both sides only need outbound HTTPS | 1-3 s |

- **Address cards**: each node publishes an encrypted card (its addresses and
  agent snapshot) to the rendezvous topic every 60 s and subscribes to the
  same topic. Peers probe the listed addresses and keep the fastest working one.
- **Relay**: when no direct path works, envelopes are published to the peer's
  private sub-topic and the peer pulls them from its long-poll stream. The
  rendezvous server only ever sees ciphertext.
- **Rendezvous server**: public `https://ntfy.sh` by default (no account).
  Self-host with `python3 relay_server.py --port 9140` on any machine both
  sides can reach, then set `rendezvous_url` in `node.json` (or pass
  `--rendezvous` to `node install`) before running `node invite`.

## Security

- Every envelope that leaves the machine is encrypted and authenticated
  (`core/crypto.py`: HMAC-SHA256 keystream + encrypt-then-MAC).
- Direct HTTP requests carry a signature over node id, timestamp, nonce,
  method, path and body digest; the window is ±120 s and nonces are rejected
  on replay. Unsigned requests get `401` with no information.
- The loopback API (`/api/v1/*`) answers only to 127.0.0.1.
- cc-connect webhook ports are never exposed; all cross-node traffic uses port
  9130 or the rendezvous server.
- Anyone holding the invite string can join the cluster. Share it privately.
  To rotate, delete `cluster.secret` on one node, run `node restart`, then
  re-invite the others.

## Files

| Path | Purpose |
| --- | --- |
| `node.json` | node id, name, ports, rendezvous url (created automatically) |
| `cluster.secret` | cluster secret (mode 600) |
| `state.db` | adds `peers`, `remote_agents`, `peer_outbox`, `peer_inbox_seen`; `messages` gains `direction/origin_node/target_node` |
| `noded.py` | the daemon |
| `relay_server.py` | optional self-hosted rendezvous |
| `/tmp/cc-relay-hub-node.log` | service log (macOS launchd / Linux systemd) |

## Operations

```bash
cc-relay-hub node status [--json]   # peers, current path per peer, outbox backlog
cc-relay-hub node doctor [--json]   # rendezvous reachability, daemon, ports, beacon
cc-relay-hub peer list|add host:port|remove <node>
cc-relay-hub inbox                  # includes queued cross-node envelopes
cc-relay-hub node restart|stop|uninstall
```

- `send` to an offline node prints `queued` and returns 0; the daemon keeps
  retrying (5 s -> 15 s -> 45 s -> 2 min -> 5 min) for up to 48 h, then marks the
  request failed and tells the origin agent. Use `--no-queue` to fail fast.
- A node whose direct path disappears falls back to relay within one probe
  cycle (60 s) and upgrades again automatically.
- `peer add host:port` adds a direct address by hand, useful when one side has
  a port forward and the other cannot see it on a card.
- Two cluster-mates on the same machine (testing) need different
  `listen_port` values and `CC_RELAY_HUB_DIR` pointing at separate hub dirs.

## Message flow

```text
A: send codex-bot@office  ->  /api/v1/send  ->  peer_outbox  ->  direct POST /peer/v1/inbox  or  relay topic
B: decrypt+verify -> messages(inbound, origin_node=A) -> deliver_to_local_agent (busy check, marker ack) -> receipt
B: agent reply [cc-relay reply_to=id] -> hook-server -> hub _on_hook -> forward_reply_to_origin_node -> peer_outbox
A: reply envelope -> messages(outbound) replied -> queue_and_attempt_reply_notification -> origin agent session
```
