#!/usr/bin/env python3
"""cc-relay-hub node daemon: cross-machine message center.

One process per machine. It
  - keeps a durable per-peer outbox and delivers envelopes over the best path
    (LAN direct -> WAN direct -> rendezvous relay),
  - receives envelopes from other nodes and hands requests to local agents via
    hub.deliver_to_local_agent, replies via hub.queue_and_attempt_reply_notification,
  - publishes an address card + agent snapshot to the rendezvous topic and
    learns peers from theirs (plus LAN UDP beacons),
  - exposes a loopback API for the CLI (/api/v1/*).

Everything that leaves this machine is encrypted and authenticated with keys
derived from the cluster secret (core/crypto.py).
"""

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
if SOURCE_DIR not in sys.path:
    sys.path.insert(0, SOURCE_DIR)

from core import crypto, node_identity, peer_protocol  # noqa: E402
from core.rendezvous import NtfyRendezvous, RendezvousError, StreamThread  # noqa: E402
from core.state import StateStore  # noqa: E402

VERSION = "0.1"
HELLO_DIRECT_SECS = 15.0
CARD_PUBLISH_SECS = 60.0
PROBE_SECS = 60.0
BEACON_SECS = 5.0
DRAIN_TICK_SECS = 5.0
DIRECT_OK_TTL_SECS = 150.0
PEER_OFFLINE_SECS = 180.0
ENVELOPE_MAX_AGE_SECS = 600.0
SIG_WINDOW_SECS = 120.0
RELAY_RETRY_BASE_SECS = 45.0
REQUEST_TTL_SECS = 48 * 3600.0
DIRECT_TIMEOUT_SECS = {"request": 40.0, "notice": 40.0, "reply": 20.0, "followup": 20.0, "receipt": 10.0, "hello": 10.0}


def _log(message):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    print("[%s] %s" % (stamp, message), flush=True)


def _backoff(attempts):
    steps = [5.0, 15.0, 45.0, 120.0, 300.0]
    return steps[min(max(attempts - 1, 0), len(steps) - 1)]


def _is_private_ip(ip):
    return ip.startswith("10.") or ip.startswith("192.168.") or (
        ip.startswith("172.") and ip.split(".")[1].isdigit() and 16 <= int(ip.split(".")[1]) <= 31
    )


def _is_cgnat_vpn_ip(ip):
    if not ip.startswith("100."):
        return False
    try:
        second = int(ip.split(".")[1])
    except (IndexError, ValueError):
        return False
    return 64 <= second <= 127


def path_label(addr):
    ip = (addr or "").rsplit(":", 1)[0]
    if ip.startswith("127.") or ip == "localhost":
        return "lan"
    if _is_cgnat_vpn_ip(ip):
        return "vpn"
    if _is_private_ip(ip):
        return "lan"
    return "wan"


def _broadcast_addrs():
    """Subnet-directed broadcast addresses (macOS often rejects 255.255.255.255)."""
    addrs = []
    try:
        if os.name != "nt":
            output = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
            for match in re.finditer(r"broadcast\s+([0-9.]+)", output):
                if match.group(1) not in addrs:
                    addrs.append(match.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    addrs.append("255.255.255.255")
    return addrs


def _local_ips():
    ips = []

    def add(ip):
        if not ip or ":" in ip or ip.startswith("127.") or ip.startswith("169.254."):
            return
        if ip not in ips:
            ips.append(ip)

    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 53))
        add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            add(ip)
    except (socket.gaierror, OSError):
        pass
    try:
        if os.name == "nt":
            # pythonw has no console to inherit; suppress a new window on each probe.
            output = subprocess.run(
                ["ipconfig"], capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            ).stdout
            for match in re.finditer(r"IPv4[^:]*:\s*([0-9.]+)", output):
                add(match.group(1))
        else:
            output = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
            for match in re.finditer(r"inet\s+([0-9.]+)", output):
                add(match.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    return ips


class NodeDaemon(object):
    def __init__(
        self,
        hub_dir,
        deliver_fn=None,
        notify_fn=None,
        snapshot_fn=None,
        rendezvous=None,
        enable_beacon=True,
        listen_host=None,
        listen_port=None,
        log=_log,
    ):
        self.hub_dir = str(hub_dir)
        self.log = log
        self.node = node_identity.ensure_node(self.hub_dir)
        if listen_port is not None:
            self.node["listen_port"] = int(listen_port)
        if listen_host:
            self.node["listen_host"] = listen_host
        self.node_id = self.node["node_id"]
        self.name = self.node["name"]
        self.port = int(self.node["listen_port"])
        self.secret = node_identity.ensure_secret(self.hub_dir, self.node)
        self.keys = node_identity.ClusterKeys(self.secret)
        self.store = StateStore(os.path.join(self.hub_dir, "state.db"))
        self.rendezvous = rendezvous or NtfyRendezvous(self.node.get("rendezvous_url") or node_identity.DEFAULT_RENDEZVOUS_URL)
        self.enable_beacon = enable_beacon
        self.deliver_fn = deliver_fn or self._default_deliver
        self.notify_fn = notify_fn or self._default_notify
        self.snapshot_fn = snapshot_fn or self._default_snapshot

        self.started_at = time.time()
        self.stop_event = threading.Event()
        self.kick_event = threading.Event()
        self.lock = threading.Lock()
        self.nonces = {}
        self.probe_now = set()
        self.last_direct_hello = {}
        self.last_card_at = 0.0
        self.last_card_error = None
        self.stream = None
        self.server = None
        self.threads = []
        self.lan_seen = {}
        self._snapshot_cache = (0.0, [])
        self._snapshot_version = int(time.time())
        self._relay_reassembler = peer_protocol.Reassembler(self.keys.enc_key, max_age_secs=ENVELOPE_MAX_AGE_SECS)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self):
        self.server = ThreadingHTTPServer((self.node.get("listen_host") or "0.0.0.0", self.port), _make_handler(self))
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self._spawn("http", self.server.serve_forever)
        self._start_stream()
        self._spawn("announce", self._announce_loop)
        self._spawn("drain", self._drain_loop)
        if self.enable_beacon:
            self._spawn("beacon-rx", self._beacon_rx_loop)
            self._spawn("beacon-tx", self._beacon_tx_loop)
        self.log("node %s (%s) listening on %s:%d, cluster %s, rendezvous %s" % (
            self.name, self.node_id, self.node.get("listen_host") or "0.0.0.0", self.port,
            self.keys.fingerprint, self.rendezvous.base_url,
        ))
        self._write_pidfile()

    def _spawn(self, name, target):
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self.threads.append(thread)
        return thread

    def _start_stream(self):
        if self.stream:
            self.stream.stop()
        since = str(int(time.time()) - 120)
        self.stream = StreamThread(
            self.rendezvous,
            [self.keys.topic, self.keys.node_topic(self.node_id)],
            since,
            self._on_relay_message,
            on_error=lambda err: self.log("rendezvous stream error: %s" % err),
        )
        self.stream.start()

    def stop(self):
        self.stop_event.set()
        self.kick_event.set()
        if self.stream:
            self.stream.stop()
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except OSError:
                pass
        self._remove_pidfile()

    def run_forever(self):
        self.start()
        try:
            while not self.stop_event.is_set():
                self.stop_event.wait(1.0)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def _pidfile(self):
        return os.path.join(self.hub_dir, "node.pid")

    def _write_pidfile(self):
        try:
            with open(self._pidfile(), "w", encoding="utf-8") as handle:
                handle.write("%d\n" % os.getpid())
        except OSError:
            pass

    def _remove_pidfile(self):
        try:
            os.remove(self._pidfile())
        except OSError:
            pass

    def reload_cluster(self):
        """Re-read node.json and the cluster secret (after `node join`)."""
        self.node = node_identity.ensure_node(self.hub_dir)
        self.secret = node_identity.ensure_secret(self.hub_dir, self.node)
        self.keys = node_identity.ClusterKeys(self.secret)
        self.rendezvous = NtfyRendezvous(self.node.get("rendezvous_url") or node_identity.DEFAULT_RENDEZVOUS_URL)
        self._relay_reassembler = peer_protocol.Reassembler(self.keys.enc_key, max_age_secs=ENVELOPE_MAX_AGE_SECS)
        self._start_stream()
        self.last_card_at = 0.0
        self.log("cluster reloaded: fingerprint %s rendezvous %s" % (self.keys.fingerprint, self.rendezvous.base_url))

    # ------------------------------------------------------------------
    # Default local integrations (hub.py)
    # ------------------------------------------------------------------
    def _hub(self):
        os.environ.setdefault("CC_RELAY_HUB_DIR", self.hub_dir)
        import hub
        return hub

    def _default_snapshot(self):
        hub = self._hub()
        registry, bindings = hub.ensure_registry_and_bindings()
        agents = []
        for name in sorted(registry.get("agents", {}).keys()):
            try:
                agent = hub.get_agent(registry, bindings, name)
            except KeyError:
                continue
            health = ""
            try:
                status = hub.get_provider(agent).get_health()
                health = status.agent_status or status.provider_status or ""
            except Exception as exc:  # noqa: BLE001 - health is best effort
                health = "unknown (%s)" % exc.__class__.__name__
            binding = agent.get("binding") or {}
            if agent.get("provider") == "cc_connect" and not int(binding.get("webhook_port", 0) or 0):
                health = "no-webhook"
            elif agent.get("provider") == "cc_connect" and not binding.get("session_key"):
                health = "no-session"
            agents.append({
                "name": name,
                "type": agent.get("type"),
                "provider": agent.get("provider"),
                "work_dir": agent.get("work_dir"),
                "groups": hub.get_agent_groups(registry, name),
                "has_session": bool(binding.get("session_key")) or agent.get("provider") == "cdp",
                "health": health,
            })
        return agents

    def _default_deliver(self, payload, origin_node, envelope_id):
        hub = self._hub()
        registry, bindings = hub.ensure_registry_and_bindings()
        try:
            agent = hub.get_agent(registry, bindings, payload["agent"])
        except KeyError as exc:
            return {"status": "failed", "request_id": payload.get("request_id"), "error": "Error: %s" % exc}
        origin_label = payload.get("origin_label") or ""
        body = payload["body"]
        if origin_label:
            body = "Origin: %s (another computer, via cc-relay-hub)\n\n%s" % (origin_label, body)
        return hub.deliver_to_local_agent(
            agent,
            body,
            expect_reply=bool(payload.get("expect_reply", True)),
            timeout=int(payload.get("timeout") or 300),
            origin_project=payload.get("origin_project"),
            origin_session=payload.get("origin_session_key"),
            request_id=payload.get("request_id"),
            state=self.store,
            origin_node=origin_node,
            envelope_id=envelope_id,
        )

    def _default_notify(self, message, reply_text, label=None):
        hub = self._hub()
        return hub.queue_and_attempt_reply_notification(self.store, message, reply_text, label=label)

    # ------------------------------------------------------------------
    # Hello / address card
    # ------------------------------------------------------------------
    def local_addrs(self):
        addrs = ["%s:%d" % (ip, self.port) for ip in _local_ips()]
        for extra in self.node.get("advertise_addrs") or []:
            if extra not in addrs:
                addrs.append(extra)
        return addrs

    def snapshot(self, max_age=30.0):
        with self.lock:
            cached_at, cached = self._snapshot_cache
        if time.time() - cached_at < max_age:
            return cached
        try:
            agents = self.snapshot_fn() or []
        except Exception as exc:  # noqa: BLE001
            self.log("snapshot error: %s" % exc)
            agents = cached
        with self.lock:
            self._snapshot_cache = (time.time(), agents)
            self._snapshot_version = int(time.time())
        return agents

    def build_hello(self):
        peers = []
        for peer in self.store.list_peers():
            peers.append({"node_id": peer["node_id"], "name": peer["name"], "addrs": peer["addrs"][:6]})
        payload = {
            "name": self.name,
            "version": VERSION,
            "addrs": self.local_addrs(),
            "snapshot_version": self._snapshot_version,
            "agents": self.snapshot(),
            "peers": peers,
            "fingerprint": self.keys.fingerprint,
        }
        return peer_protocol.new_envelope("hello", self.node_id, "*", payload, from_name=self.name)

    # ------------------------------------------------------------------
    # Inbound envelope handling
    # ------------------------------------------------------------------
    def _receipt(self, to_node, ref_envelope, payload):
        payload = dict(payload)
        payload["ref"] = ref_envelope["envelope_id"]
        payload["ref_kind"] = ref_envelope["kind"]
        return peer_protocol.new_envelope("receipt", self.node_id, to_node, payload, from_name=self.name)

    def handle_envelope(self, envelope, via="direct", remote_ip=None, remote_port=None):
        kind = envelope.get("kind")
        from_node = envelope.get("from_node") or ""
        to_node = envelope.get("to_node") or "*"
        if not from_node or from_node == self.node_id:
            return None
        if to_node not in ("*", self.node_id):
            return None
        if kind == "hello":
            return self._handle_hello(envelope, via, remote_ip, remote_port)
        if kind in ("request", "notice"):
            return self._handle_request(envelope, via)
        if kind in ("reply", "followup"):
            return self._handle_reply(envelope, via)
        if kind == "receipt":
            self._handle_receipt(envelope, via)
            return None
        return None

    def _handle_hello(self, envelope, via, remote_ip, remote_port):
        payload = envelope.get("payload") or {}
        from_node = envelope["from_node"]
        name = payload.get("name") or envelope.get("from_name") or from_node
        addrs = [a for a in (payload.get("addrs") or []) if isinstance(a, str)]
        if remote_ip and remote_port:
            observed = "%s:%s" % (remote_ip, remote_port)
            if observed not in addrs:
                addrs.insert(0, observed)
        existing = self.store.get_peer(from_node)
        self.store.upsert_peer(from_node, name=name, addrs=addrs or None, snapshot_version=payload.get("snapshot_version"))
        agents = payload.get("agents")
        if isinstance(agents, list):
            self.store.replace_remote_agents(from_node, agents)
        for gossip in payload.get("peers") or []:
            node_id = (gossip or {}).get("node_id")
            if not node_id or node_id == self.node_id or self.store.get_peer(node_id):
                continue
            self.store.upsert_peer(node_id, name=gossip.get("name"), addrs=gossip.get("addrs") or [], now=0.0)
            self.probe_now.add(node_id)
        if not existing or not (existing.get("last_direct_ok_at") or 0) or set(addrs) - set(existing.get("addrs") or []):
            self.probe_now.add(from_node)
        if via == "direct":
            return self.build_hello()
        return None

    def _handle_request(self, envelope, via):
        from_node = envelope["from_node"]
        envelope_id = envelope["envelope_id"]
        payload = envelope.get("payload") or {}
        if not self.store.record_inbound_envelope(envelope_id, envelope["kind"], from_node):
            cached = self.store.get_inbound_result(envelope_id)
            if cached:
                return self._receipt(from_node, envelope, cached)
            return None
        try:
            result = self.deliver_fn(payload, from_node, envelope_id) or {}
        except Exception as exc:  # noqa: BLE001
            self.log("deliver error for %s: %s" % (envelope_id, exc))
            result = {"status": "failed", "error": "Error: local delivery raised %s" % exc.__class__.__name__}
        receipt = {
            "request_id": payload.get("request_id") or result.get("request_id"),
            "status": result.get("status") or "failed",
            "error": result.get("error"),
            "delivered_at": result.get("delivered_at"),
            "agent": payload.get("agent"),
        }
        self.store.set_inbound_result(envelope_id, receipt)
        self.log("%s %s from %s -> %s: %s" % (
            envelope["kind"], receipt["request_id"], envelope.get("from_name") or from_node,
            payload.get("agent"), receipt["status"],
        ))
        return self._receipt(from_node, envelope, receipt)

    def _handle_reply(self, envelope, via):
        from_node = envelope["from_node"]
        envelope_id = envelope["envelope_id"]
        payload = envelope.get("payload") or {}
        if not self.store.record_inbound_envelope(envelope_id, envelope["kind"], from_node):
            cached = self.store.get_inbound_result(envelope_id)
            return self._receipt(from_node, envelope, cached) if cached else None
        request_id = payload.get("request_id") or ""
        reply_text = payload.get("reply") or ""
        message = self.store.get_message(request_id)
        if not message or message.get("direction") != "outbound":
            receipt = {"request_id": request_id, "status": "unmatched"}
            self.store.set_inbound_result(envelope_id, receipt)
            return self._receipt(from_node, envelope, receipt)
        label = payload.get("label") or ("follow-up" if envelope["kind"] == "followup" else None)
        if message.get("status") in ("pending", "delivered") or envelope["kind"] == "reply":
            self.store.mark_replied(request_id, reply_text, float(envelope.get("ts") or time.time()))
        else:
            self.store.mark_replied(request_id, reply_text, float(envelope.get("ts") or time.time()))
        self.store.append_event(
            event_type="message.remote_reply",
            agent_id=message.get("target") or "",
            request_id=request_id,
            session_key=message.get("session_key"),
            content=reply_text,
            timestamp=float(envelope.get("ts") or time.time()),
        )
        try:
            notify = self.notify_fn(message, reply_text, label) or {}
        except Exception as exc:  # noqa: BLE001
            self.log("notify error for %s: %s" % (request_id, exc))
            notify = {"status": "failed", "error": str(exc)}
        receipt = {"request_id": request_id, "status": "accepted", "notify": notify.get("status")}
        self.store.set_inbound_result(envelope_id, receipt)
        self.log("%s for %s from %s: notify=%s" % (envelope["kind"], request_id, envelope.get("from_name") or from_node, notify.get("status")))
        return self._receipt(from_node, envelope, receipt)

    def _handle_receipt(self, envelope, via):
        payload = envelope.get("payload") or {}
        ref = payload.get("ref")
        if not ref:
            return
        item = self.store.get_peer_outbox(ref)
        if not item:
            return
        if item["status"] == "pending":
            # Keep the path the envelope itself travelled (set at dispatch time);
            # the receipt may legitimately come back over a different path.
            self.store.mark_peer_outbox_delivered(ref, via=None)
        if item["kind"] in ("request", "notice"):
            request_id = payload.get("request_id") or (item.get("envelope") or {}).get("payload", {}).get("request_id")
            message = self.store.get_message(request_id) if request_id else None
            if message and message.get("status") == "pending":
                if payload.get("status") == "delivered":
                    self.store.mark_delivered(request_id, float(payload.get("delivered_at") or time.time()))
                else:
                    self.store.mark_failed(request_id, payload.get("error") or payload.get("status") or "failed")

    # ------------------------------------------------------------------
    # Relay stream
    # ------------------------------------------------------------------
    def _on_relay_message(self, event):
        try:
            envelope = self._relay_reassembler.feed(event.get("message") or "")
        except ValueError as exc:
            self.log("relay message rejected: %s" % exc)
            return
        if envelope is None:
            return
        try:
            response = self.handle_envelope(envelope, via="relay")
        except Exception as exc:  # noqa: BLE001
            self.log("relay handler error: %s" % exc)
            return
        if response is not None:
            self.store.enqueue_peer_envelope(response, ttl_secs=3600)
            self.kick_event.set()

    def publish_card(self):
        envelope = self.build_hello()
        chunks = peer_protocol.to_chunks(envelope, self.keys.enc_key)
        last_error = None
        for attempt in range(3):
            try:
                for chunk in chunks:
                    self.rendezvous.publish(self.keys.topic, chunk)
                self.last_card_at = time.time()
                self.last_card_error = None
                return
            except RendezvousError as exc:
                # Public ntfy occasionally drops a TLS connection; retry before logging.
                last_error = exc
                self.stop_event.wait(2.0 * (attempt + 1))
        self.last_card_error = str(last_error)
        self.log("address card publish failed after retries: %s" % last_error)

    # ------------------------------------------------------------------
    # Direct HTTP transport
    # ------------------------------------------------------------------
    def _signed_headers(self, method, path, body):
        ts = "%.3f" % time.time()
        nonce = uuid.uuid4().hex
        signature = crypto.sign(self.keys.auth_key, self.node_id, ts, nonce, method, path, crypto.body_digest(body))
        return {
            "Content-Type": "text/plain",
            "X-CC-Relay-Node": self.node_id,
            "X-CC-Relay-Port": str(self.port),
            "X-CC-Relay-Ts": ts,
            "X-CC-Relay-Nonce": nonce,
            "X-CC-Relay-Sig": signature,
        }

    def verify_request(self, headers, method, path, body):
        node = headers.get("X-CC-Relay-Node") or ""
        ts = headers.get("X-CC-Relay-Ts") or ""
        nonce = headers.get("X-CC-Relay-Nonce") or ""
        signature = headers.get("X-CC-Relay-Sig") or ""
        try:
            if abs(time.time() - float(ts)) > SIG_WINDOW_SECS:
                return None
        except ValueError:
            return None
        if not crypto.verify(self.keys.auth_key, signature, node, ts, nonce, method, path, crypto.body_digest(body)):
            return None
        with self.lock:
            now = time.time()
            stale = [key for key, seen in self.nonces.items() if now - seen > SIG_WINDOW_SECS * 2]
            for key in stale:
                del self.nonces[key]
            if nonce in self.nonces:
                return None
            self.nonces[nonce] = now
        return node

    def direct_post(self, addr, path, envelope, timeout):
        body = "\n".join(peer_protocol.to_chunks(envelope, self.keys.enc_key))
        request = urllib.request.Request(
            "http://%s%s" % (addr, path), data=body.encode("utf-8"), headers=self._signed_headers("POST", path, body), method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", errors="replace")
        reassembler = peer_protocol.Reassembler(self.keys.enc_key, max_age_secs=ENVELOPE_MAX_AGE_SECS)
        return reassembler.feed_text(text)

    def direct_probe(self, addr, timeout=4.0):
        path = "/peer/v1/probe"
        request = urllib.request.Request("http://%s%s" % (addr, path), headers=self._signed_headers("GET", path, b""))
        started = time.time()
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
        return data, (time.time() - started) * 1000.0

    def probe_peer(self, peer):
        best = None
        for addr in peer.get("addrs") or []:
            try:
                data, rtt = self.direct_probe(addr)
            except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, OSError, ValueError):
                continue
            if data.get("node_id") != peer["node_id"]:
                continue
            if best is None or rtt < best[1]:
                best = (addr, rtt)
        if best:
            self.store.set_peer_probe(peer["node_id"], best[0], path_label(best[0]), best[1], True)
            if not peer.get("best_addr") or peer.get("best_addr") != best[0]:
                self.log("peer %s reachable directly at %s (%s, %.0f ms)" % (peer["name"], best[0], path_label(best[0]), best[1]))
            self.kick_event.set()
        else:
            if peer.get("best_addr"):
                self.log("peer %s lost direct path, falling back to relay" % peer["name"])
            self.store.set_peer_probe(peer["node_id"], None, "relay", None, False)
        return best

    def choose_path(self, peer):
        now = time.time()
        if peer and peer.get("best_addr") and (now - float(peer.get("last_direct_ok_at") or 0)) <= DIRECT_OK_TTL_SECS:
            return "direct", peer["best_addr"], peer.get("path") or path_label(peer["best_addr"])
        return "relay", None, "relay"

    # ------------------------------------------------------------------
    # Outbox
    # ------------------------------------------------------------------
    def _drain_loop(self):
        while not self.stop_event.is_set():
            self.kick_event.wait(DRAIN_TICK_SECS)
            self.kick_event.clear()
            if self.stop_event.is_set():
                break
            try:
                self.drain_once()
            except Exception as exc:  # noqa: BLE001
                self.log("drain error: %s" % exc)

    def drain_once(self):
        now = time.time()
        for item in self.store.claim_peer_outbox(due_at=now, limit=50):
            if self.stop_event.is_set():
                return
            self._dispatch(item, now)

    def _dispatch(self, item, now=None):
        now = time.time() if now is None else now
        envelope = item.get("envelope")
        if not envelope:
            self.store.mark_peer_outbox_status(item["envelope_id"], "failed", "corrupt envelope")
            return
        if item.get("expires_at") and now > float(item["expires_at"]):
            self.store.mark_peer_outbox_status(item["envelope_id"], "expired", "peer unreachable until expiry")
            self._expire_request(item)
            return
        peer = self.store.get_peer(item["to_node"])
        mode, addr, label = self.choose_path(peer)
        kind = item["kind"]
        if mode == "direct":
            try:
                responses = self.direct_post(addr, "/peer/v1/inbox", envelope, DIRECT_TIMEOUT_SECS.get(kind, 20.0))
            except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, OSError, ValueError) as exc:
                self.log("direct send to %s@%s failed (%s); retrying via relay" % (peer["name"] if peer else item["to_node"], addr, exc))
                self.store.set_peer_probe(item["to_node"], None, "relay", None, False)
                self.probe_now.add(item["to_node"])
                mode = "relay"
            else:
                self.store.mark_peer_outbox_delivered(item["envelope_id"], via=label)
                for response in responses:
                    self.handle_envelope(response, via="direct")
                if kind in ("request", "notice") and not responses:
                    self._mark_request_delivered_without_receipt(envelope)
                return
        try:
            for chunk in peer_protocol.to_chunks(envelope, self.keys.enc_key):
                self.rendezvous.publish(self.keys.node_topic(item["to_node"]), chunk)
        except RendezvousError as exc:
            attempts = int(item.get("attempts") or 0) + 1
            self.store.mark_peer_outbox_attempt(item["envelope_id"], now + _backoff(attempts), error=str(exc), via="relay")
            return
        if kind in ("receipt", "hello"):
            self.store.mark_peer_outbox_delivered(item["envelope_id"], via="relay")
            return
        attempts = int(item.get("attempts") or 0) + 1
        retry_in = min(RELAY_RETRY_BASE_SECS * attempts, 600.0)
        self.store.mark_peer_outbox_attempt(item["envelope_id"], now + retry_in, error="awaiting receipt via relay", via="relay")

    def _mark_request_delivered_without_receipt(self, envelope):
        request_id = (envelope.get("payload") or {}).get("request_id")
        message = self.store.get_message(request_id) if request_id else None
        if message and message.get("status") == "pending":
            self.store.mark_delivered(request_id, time.time())

    def _expire_request(self, item):
        envelope = item.get("envelope") or {}
        if item["kind"] not in ("request", "notice"):
            return
        request_id = (envelope.get("payload") or {}).get("request_id")
        message = self.store.get_message(request_id) if request_id else None
        if not message or message.get("status") != "pending":
            return
        self.store.mark_failed(request_id, "expired: peer node unreachable")
        peer = self.store.get_peer(item["to_node"]) or {}
        text = "Delivery of request %s to %s@%s failed: the node stayed unreachable until the request expired." % (
            request_id, message.get("target"), peer.get("name") or item["to_node"],
        )
        try:
            self.notify_fn(message, text, "delivery-failed")
        except Exception as exc:  # noqa: BLE001
            self.log("expiry notice failed: %s" % exc)

    # ------------------------------------------------------------------
    # Announce / probe loop
    # ------------------------------------------------------------------
    def _announce_loop(self):
        while not self.stop_event.is_set():
            try:
                self.announce_once()
            except Exception as exc:  # noqa: BLE001
                self.log("announce error: %s" % exc)
            self.stop_event.wait(5.0)

    def announce_once(self):
        now = time.time()
        if now - self.last_card_at >= CARD_PUBLISH_SECS:
            self.publish_card()
        for peer in self.store.list_peers():
            node_id = peer["node_id"]
            probe_due = node_id in self.probe_now or (now - float(peer.get("last_probe_at") or 0)) >= PROBE_SECS
            if probe_due and peer.get("addrs"):
                self.probe_now.discard(node_id)
                peer_after = self.store.get_peer(node_id) or peer
                self.probe_peer(peer_after)
                peer = self.store.get_peer(node_id) or peer
            mode, addr, _label = self.choose_path(peer)
            if mode == "direct" and now - self.last_direct_hello.get(node_id, 0.0) >= HELLO_DIRECT_SECS:
                self.last_direct_hello[node_id] = now
                try:
                    responses = self.direct_post(addr, "/peer/v1/hello", self.build_hello(), DIRECT_TIMEOUT_SECS["hello"])
                    for response in responses:
                        self.handle_envelope(response, via="direct")
                except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, OSError, ValueError) as exc:
                    self.log("direct hello to %s failed: %s" % (peer["name"], exc))
                    self.store.set_peer_probe(node_id, None, "relay", None, False)

    # ------------------------------------------------------------------
    # LAN beacon
    # ------------------------------------------------------------------
    def _beacon_payload(self):
        return json.dumps({
            "v": 1,
            "node_id": self.node_id,
            "name": self.name,
            "port": self.port,
            "fp": self.keys.fingerprint,
            "sig": crypto.sign(self.keys.auth_key, "beacon", self.node_id, str(self.port))[:32],
        }).encode("utf-8")

    def _beacon_tx_loop(self):
        port = int(self.node.get("beacon_port") or node_identity.DEFAULT_BEACON_PORT)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        targets = _broadcast_addrs()
        refreshed_at = time.time()
        while not self.stop_event.is_set():
            if time.time() - refreshed_at > 300:
                targets = _broadcast_addrs()
                refreshed_at = time.time()
            payload = self._beacon_payload()
            for target in targets:
                try:
                    sock.sendto(payload, (target, port))
                except OSError:
                    continue
            self.stop_event.wait(BEACON_SECS)
        sock.close()

    def _beacon_rx_loop(self):
        port = int(self.node.get("beacon_port") or node_identity.DEFAULT_BEACON_PORT)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        try:
            sock.bind(("", port))
        except OSError as exc:
            self.log("beacon listener disabled: %s" % exc)
            return
        sock.settimeout(1.0)
        while not self.stop_event.is_set():
            try:
                data, (ip, _port) = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                info = json.loads(data.decode("utf-8"))
            except ValueError:
                continue
            node_id = info.get("node_id")
            if not node_id or node_id == self.node_id:
                continue
            if info.get("fp") != self.keys.fingerprint:
                self.lan_seen[node_id] = {"node_id": node_id, "name": info.get("name"), "ip": ip, "port": info.get("port"), "seen_at": time.time()}
                continue
            expected = crypto.sign(self.keys.auth_key, "beacon", node_id, str(info.get("port")))[:32]
            if info.get("sig") != expected:
                continue
            addr = "%s:%s" % (ip, info.get("port"))
            peer = self.store.get_peer(node_id)
            if not peer or addr not in (peer.get("addrs") or []):
                self.store.add_peer_addr(node_id, addr, name=info.get("name"))
                self.probe_now.add(node_id)
        sock.close()

    # ------------------------------------------------------------------
    # Loopback API
    # ------------------------------------------------------------------
    def api_send(self, payload):
        agent = (payload.get("agent") or "").strip()
        node_id = (payload.get("node_id") or "").strip()
        body = payload.get("body") or ""
        if not agent or not node_id or not body.strip():
            return 400, {"error": "agent, node_id and body are required"}
        peer = self.store.get_peer(node_id)
        expect_reply = bool(payload.get("expect_reply", True))
        request_id = uuid.uuid4().hex
        now = time.time()
        node_name = (peer or {}).get("name") or node_id
        self.store.insert_message(
            request_id=request_id,
            sender=payload.get("sender") or "cc-relay",
            target=agent,
            session_key="remote:%s:%s" % (node_name, agent),
            provider="remote",
            body=body,
            status="pending",
            created_at=now,
            origin_project=payload.get("origin_project"),
            origin_session_key=payload.get("origin_session"),
            direction="outbound",
            target_node=node_id,
        )
        envelope = peer_protocol.new_envelope(
            "request" if expect_reply else "notice",
            self.node_id,
            node_id,
            {
                "request_id": request_id,
                "agent": agent,
                "body": body,
                "expect_reply": expect_reply,
                "timeout": int(payload.get("timeout") or 300),
                "origin_project": payload.get("origin_project"),
                "origin_session_key": payload.get("origin_session"),
                "sender": payload.get("sender") or "",
                "origin_label": "%s@%s" % (payload.get("sender") or "agent", self.name),
            },
            from_name=self.name,
        )
        self.store.enqueue_peer_envelope(envelope, now=now, ttl_secs=REQUEST_TTL_SECS)
        self.kick_event.set()
        deadline = now + float(payload.get("wait_secs") or 20.0)
        while time.time() < deadline:
            message = self.store.get_message(request_id)
            if message and message.get("status") != "pending":
                break
            time.sleep(0.25)
        message = self.store.get_message(request_id) or {}
        outbox = self.store.get_peer_outbox(envelope["envelope_id"]) or {}
        status = message.get("status") or "pending"
        if status == "pending":
            status = "queued"
        return 200, {
            "request_id": request_id,
            "status": status,
            "via": outbox.get("via") or "",
            "error": message.get("error") or outbox.get("last_error"),
            "node": node_name,
        }

    def api_wait(self, request_id, timeout):
        deadline = time.time() + max(float(timeout), 0.0)
        while True:
            message = self.store.get_message(request_id)
            if not message:
                return 404, {"error": "unknown request_id"}
            if message.get("status") in ("replied", "failed", "timeout"):
                return 200, {"status": message["status"], "reply_body": message.get("reply_body"), "error": message.get("error")}
            if time.time() >= deadline:
                return 200, {"status": message.get("status")}
            time.sleep(0.5)

    def api_health(self):
        peers = []
        now = time.time()
        for peer in self.store.list_peers():
            mode, addr, label = self.choose_path(peer)
            last_seen = float(peer.get("last_seen_at") or 0)
            peers.append({
                "node_id": peer["node_id"],
                "name": peer["name"],
                "online": bool(last_seen) and now - last_seen <= PEER_OFFLINE_SECS,
                "last_seen_secs": int(now - last_seen) if last_seen else None,
                "path": label if mode == "direct" else "relay",
                "addr": addr,
                "rtt_ms": peer.get("rtt_ms"),
                "addrs": peer.get("addrs") or [],
                "agents": [a["name"] for a in self.store.list_remote_agents(peer["node_id"])],
            })
        pending = self.store.list_peer_outbox(status="pending", limit=500)
        return {
            "node_id": self.node_id,
            "name": self.name,
            "version": VERSION,
            "port": self.port,
            "addrs": self.local_addrs(),
            "cluster_fingerprint": self.keys.fingerprint,
            "rendezvous": {
                "url": self.rendezvous.base_url,
                "connected": bool(self.stream and self.stream.connected_at),
                "last_error": (self.stream.last_error if self.stream else None) or self.last_card_error,
                "last_card_at": self.last_card_at,
            },
            "beacon": self.enable_beacon,
            "uptime_secs": int(now - self.started_at),
            "peers": peers,
            "outbox_pending": len(pending),
            "lan_discovered": list(self.lan_seen.values()),
        }

    def api_peer_add(self, address, name=None):
        address = (address or "").strip()
        if not address:
            return 400, {"error": "address required (host:port)"}
        if ":" not in address:
            address = "%s:%d" % (address, node_identity.DEFAULT_NODE_PORT)
        try:
            data, rtt = self.direct_probe(address, timeout=6.0)
        except (urllib.error.HTTPError,) as exc:
            if exc.code == 401:
                return 401, {"error": "peer rejected our signature: it is not in this cluster (use `node join`)"}
            return 502, {"error": "probe failed: HTTP %s" % exc.code}
        except (urllib.error.URLError, socket.timeout, OSError, ValueError) as exc:
            return 502, {"error": "probe failed: %s" % exc}
        node_id = data.get("node_id")
        if not node_id or node_id == self.node_id:
            return 400, {"error": "that address is this node"}
        self.store.add_peer_addr(node_id, address, name=name or data.get("name"))
        self.store.set_peer_probe(node_id, address, path_label(address), rtt, True)
        try:
            for response in self.direct_post(address, "/peer/v1/hello", self.build_hello(), DIRECT_TIMEOUT_SECS["hello"]):
                self.handle_envelope(response, via="direct")
        except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, OSError, ValueError) as exc:
            return 200, {"node_id": node_id, "name": data.get("name"), "warning": "probe ok but hello failed: %s" % exc}
        self.kick_event.set()
        return 200, {"node_id": node_id, "name": data.get("name"), "rtt_ms": rtt, "path": path_label(address)}


def _make_handler(daemon):
    class Handler(BaseHTTPRequestHandler):
        server_version = "cc-relay-hub-node/%s" % VERSION
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def _json(self, code, payload):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _text(self, code, text):
            data = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _is_loopback(self):
            return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if path.startswith("/api/v1/"):
                if not self._is_loopback():
                    return self._json(403, {"error": "loopback only"})
                if path == "/api/v1/health":
                    return self._json(200, daemon.api_health())
                if path == "/api/v1/peers":
                    return self._json(200, {"peers": daemon.api_health()["peers"]})
                if path == "/api/v1/agents":
                    return self._json(200, {"agents": daemon.store.list_remote_agents()})
                if path == "/api/v1/discover":
                    return self._json(200, {"nodes": list(daemon.lan_seen.values())})
                if path.startswith("/api/v1/messages/") and path.endswith("/wait"):
                    request_id = path[len("/api/v1/messages/"):-len("/wait")]
                    timeout = float((query.get("timeout") or ["300"])[0])
                    code, payload = daemon.api_wait(request_id, timeout)
                    return self._json(code, payload)
                return self._json(404, {"error": "not found"})
            if path.startswith("/peer/v1/"):
                node = daemon.verify_request(self.headers, "GET", path, b"")
                if not node:
                    return self._json(401, {"error": "unauthorized"})
                if path == "/peer/v1/probe":
                    return self._json(200, {"node_id": daemon.node_id, "name": daemon.name, "time": time.time(), "version": VERSION})
                if path.startswith("/peer/v1/agents/"):
                    wanted = path[len("/peer/v1/agents/"):]
                    for agent in daemon.snapshot(max_age=5.0):
                        if agent.get("name") == wanted:
                            return self._json(200, agent)
                    return self._json(404, {"error": "unknown agent"})
                return self._json(404, {"error": "not found"})
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path
            body = self._body()
            if path.startswith("/api/v1/"):
                if not self._is_loopback():
                    return self._json(403, {"error": "loopback only"})
                try:
                    payload = json.loads(body.decode("utf-8")) if body else {}
                except ValueError:
                    return self._json(400, {"error": "invalid json"})
                if path == "/api/v1/send":
                    code, result = daemon.api_send(payload)
                    return self._json(code, result)
                if path == "/api/v1/kick":
                    daemon.kick_event.set()
                    return self._json(200, {"ok": True})
                if path == "/api/v1/reload":
                    daemon.reload_cluster()
                    return self._json(200, {"ok": True, "cluster_fingerprint": daemon.keys.fingerprint})
                if path == "/api/v1/peers/add":
                    code, result = daemon.api_peer_add(payload.get("address"), payload.get("name"))
                    return self._json(code, result)
                if path == "/api/v1/peers/remove":
                    peer = daemon.store.find_peer_by_name(payload.get("node") or "")
                    if not peer:
                        return self._json(404, {"error": "unknown peer"})
                    daemon.store.remove_peer(peer["node_id"])
                    return self._json(200, {"removed": peer["node_id"]})
                if path == "/api/v1/probe":
                    peer = daemon.store.find_peer_by_name(payload.get("node") or "")
                    if not peer:
                        return self._json(404, {"error": "unknown peer"})
                    best = daemon.probe_peer(peer)
                    return self._json(200, {"direct": bool(best), "addr": best[0] if best else None, "rtt_ms": best[1] if best else None})
                if path == "/api/v1/publish":
                    daemon.publish_card()
                    return self._json(200, {"ok": daemon.last_card_error is None, "error": daemon.last_card_error})
                return self._json(404, {"error": "not found"})
            if path.startswith("/peer/v1/"):
                node = daemon.verify_request(self.headers, "POST", path, body)
                if not node:
                    return self._json(401, {"error": "unauthorized"})
                if path in ("/peer/v1/inbox", "/peer/v1/hello"):
                    reassembler = peer_protocol.Reassembler(daemon.keys.enc_key, max_age_secs=ENVELOPE_MAX_AGE_SECS)
                    try:
                        envelopes = reassembler.feed_text(body.decode("utf-8", errors="replace"))
                    except ValueError:
                        return self._json(400, {"error": "undecodable envelope"})
                    remote_port = self.headers.get("X-CC-Relay-Port")
                    responses = []
                    for envelope in envelopes:
                        response = daemon.handle_envelope(envelope, via="direct", remote_ip=self.client_address[0], remote_port=remote_port)
                        if response is not None:
                            responses.extend(peer_protocol.to_chunks(response, daemon.keys.enc_key))
                    return self._text(200, "\n".join(responses))
                return self._json(404, {"error": "not found"})
            return self._json(404, {"error": "not found"})

    return Handler


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="cc-relay-hub node daemon")
    parser.add_argument("--hub-dir", default=os.environ.get("CC_RELAY_HUB_DIR") or os.path.join(os.path.expanduser("~"), ".cc-connect", "cc-relay-hub"))
    parser.add_argument("--no-beacon", action="store_true")
    args = parser.parse_args(argv)
    daemon = NodeDaemon(args.hub_dir, enable_beacon=not args.no_beacon)
    daemon.run_forever()


if __name__ == "__main__":
    main()
