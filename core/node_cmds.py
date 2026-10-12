"""CLI handlers for `cc-relay-hub node ...` and `cc-relay-hub peer ...`."""

import json
import os
import platform
import signal
import socket
import subprocess
import sys
import time

import hub
from core import node_identity
from core.rendezvous import NtfyRendezvous, RendezvousError
from core.state import StateStore

SERVICE_LABEL = "com.cc-relay-hub.node"
LOG_PATH = "/tmp/cc-relay-hub-node.log"


def _health():
    try:
        health = hub.node_api("GET", "/api/v1/health", timeout=5.0)
    except (hub.NodeDaemonUnavailable, RuntimeError):
        return None
    node = node_identity.load_node(hub.HUB_DIR)
    if node and health.get("node_id") and health["node_id"] != node.get("node_id"):
        # Another hub dir's daemon answered on this port; treat ours as not running.
        return None
    return health


def _hub_script():
    return os.path.join(hub.SOURCE_DIR, "hub.py")


def _fmt_ago(ts):
    if not ts:
        return "never"
    delta = int(time.time() - float(ts))
    if delta < 90:
        return "%ds ago" % delta
    if delta < 5400:
        return "%dm ago" % (delta // 60)
    return "%dh ago" % (delta // 3600)


# ---------------------------------------------------------------------------
# node start / install / stop
# ---------------------------------------------------------------------------
def cmd_node_start(args):
    node = node_identity.ensure_node(hub.HUB_DIR, name=args.name, port=args.port, rendezvous_url=args.rendezvous)
    node_identity.ensure_secret(hub.HUB_DIR, node)
    if _health():
        print("Error: a node daemon is already running on port %s" % node["listen_port"])
        return 1
    from noded import NodeDaemon

    daemon = NodeDaemon(hub.HUB_DIR)

    def _terminate(_signum, _frame):
        daemon.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _terminate)
        except (ValueError, OSError):
            pass
    daemon.run_forever()
    return 0


def _plist_path():
    return os.path.expanduser("~/Library/LaunchAgents/%s.plist" % SERVICE_LABEL)


def _systemd_path():
    return os.path.expanduser("~/.config/systemd/user/cc-relay-hub-node.service")


def _service_env():
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
    if os.environ.get("CC_RELAY_HUB_DIR"):
        env["CC_RELAY_HUB_DIR"] = os.environ["CC_RELAY_HUB_DIR"]
    return env


def _write_plist():
    env_xml = "".join("    <key>%s</key>\n    <string>%s</string>\n" % (k, v) for k, v in _service_env().items())
    content = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>%s</string>
  <key>ProgramArguments</key>
  <array>
    <string>%s</string>
    <string>%s</string>
    <string>node</string>
    <string>start</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
%s  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>%s</string>
  <key>StandardErrorPath</key>
  <string>%s</string>
</dict>
</plist>
""" % (SERVICE_LABEL, sys.executable, _hub_script(), env_xml, LOG_PATH, LOG_PATH)
    path = _plist_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


def _write_systemd_unit():
    env_lines = "".join("Environment=%s=%s\n" % (k, v) for k, v in _service_env().items())
    content = """[Unit]
Description=cc-relay-hub node daemon
After=network-online.target

[Service]
ExecStart=%s %s node start
Restart=always
RestartSec=5
%s
[Install]
WantedBy=default.target
""" % (sys.executable, _hub_script(), env_lines)
    path = _systemd_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def _wait_health(timeout=12.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        health = _health()
        if health:
            return health
        time.sleep(0.5)
    return None


def cmd_node_install(args):
    node = node_identity.ensure_node(hub.HUB_DIR, name=args.name, port=args.port, rendezvous_url=args.rendezvous)
    node_identity.ensure_secret(hub.HUB_DIR, node)
    system = platform.system()
    if system == "Darwin":
        path = _write_plist()
        _run(["launchctl", "unload", path])
        result = _run(["launchctl", "load", "-w", path])
        if result.returncode != 0:
            print("Error: launchctl load failed: %s" % (result.stderr.strip() or result.stdout.strip()))
            return 1
        print("Installed launchd service %s (%s)" % (SERVICE_LABEL, path))
    elif system == "Linux":
        path = _write_systemd_unit()
        for cmd in (["systemctl", "--user", "daemon-reload"], ["systemctl", "--user", "enable", "--now", "cc-relay-hub-node.service"]):
            result = _run(cmd)
            if result.returncode != 0:
                print("Error: %s failed: %s" % (" ".join(cmd), result.stderr.strip()))
                return 1
        print("Installed systemd user service cc-relay-hub-node (%s)" % path)
    elif system == "Windows":
        task = "cc-relay-hub-node"
        command = '"%s" "%s" node start' % (sys.executable, _hub_script())
        _run(["schtasks", "/Delete", "/TN", task, "/F"])
        result = _run(["schtasks", "/Create", "/TN", task, "/SC", "ONLOGON", "/TR", command, "/F"])
        if result.returncode != 0:
            print("Error: schtasks create failed: %s" % (result.stderr.strip() or result.stdout.strip()))
            return 1
        _run(["schtasks", "/Run", "/TN", task])
        print("Installed scheduled task %s" % task)
    else:
        print("Error: unsupported platform %s; run `cc-relay-hub node start` under your own supervisor" % system)
        return 1
    health = _wait_health()
    if not health:
        print("Service installed but the daemon did not answer within 12s. Check %s" % LOG_PATH)
        return 1
    print("Node %s is running (port %s, cluster %s)." % (health["name"], health["port"], health["cluster_fingerprint"]))
    print("Next: `cc-relay-hub node invite` on a machine already in the cluster, then `cc-relay-hub node join <string>` here.")
    return 0


def cmd_node_uninstall(_args):
    system = platform.system()
    if system == "Darwin":
        path = _plist_path()
        _run(["launchctl", "unload", path])
        if os.path.exists(path):
            os.remove(path)
        print("Removed %s" % SERVICE_LABEL)
    elif system == "Linux":
        _run(["systemctl", "--user", "disable", "--now", "cc-relay-hub-node.service"])
        path = _systemd_path()
        if os.path.exists(path):
            os.remove(path)
        print("Removed cc-relay-hub-node.service")
    elif system == "Windows":
        _run(["schtasks", "/End", "/TN", "cc-relay-hub-node"])
        _run(["schtasks", "/Delete", "/TN", "cc-relay-hub-node", "/F"])
        print("Removed scheduled task cc-relay-hub-node")
    return cmd_node_stop(_args)


def _service_installed():
    system = platform.system()
    if system == "Darwin":
        return os.path.exists(_plist_path())
    if system == "Linux":
        return os.path.exists(_systemd_path())
    return False


def cmd_node_stop(_args):
    system = platform.system()
    stopped = False
    if system == "Darwin" and os.path.exists(_plist_path()):
        _run(["launchctl", "unload", _plist_path()])
        stopped = True
    elif system == "Linux" and os.path.exists(_systemd_path()):
        _run(["systemctl", "--user", "stop", "cc-relay-hub-node.service"])
        stopped = True
    pidfile = os.path.join(str(hub.HUB_DIR), "node.pid")
    if os.path.exists(pidfile):
        try:
            with open(pidfile, "r", encoding="utf-8") as handle:
                pid = int(handle.read().strip() or 0)
            if pid:
                os.kill(pid, signal.SIGTERM)
                stopped = True
        except (OSError, ValueError):
            pass
    print("Node daemon stopped." if stopped else "Node daemon was not running.")
    return 0


def cmd_node_restart(args):
    system = platform.system()
    if system == "Darwin" and os.path.exists(_plist_path()):
        _run(["launchctl", "unload", _plist_path()])
        _run(["launchctl", "load", "-w", _plist_path()])
    elif system == "Linux" and os.path.exists(_systemd_path()):
        _run(["systemctl", "--user", "restart", "cc-relay-hub-node.service"])
    else:
        cmd_node_stop(args)
        print("No login service installed; start it again with `cc-relay-hub node start` or `node install`.")
        return 0
    health = _wait_health()
    if not health:
        print("Error: daemon did not come back within 12s. Check %s" % LOG_PATH)
        return 1
    print("Node %s restarted." % health["name"])
    return 0


# ---------------------------------------------------------------------------
# status / invite / join / doctor / discover
# ---------------------------------------------------------------------------
def cmd_node_status(args):
    node = node_identity.load_node(hub.HUB_DIR)
    health = _health()
    if getattr(args, "json", False):
        print(json.dumps({"node": node, "daemon": health}, indent=2, ensure_ascii=False))
        return 0 if health else 3
    if not node:
        print("Node not initialised. Run `cc-relay-hub node install` (or `node start`).")
        return 2
    secret = node_identity.load_secret(hub.HUB_DIR, node)
    fp = node_identity.ClusterKeys(secret).fingerprint if secret else "-"
    if not health:
        print("Node:        %s (%s)  cluster %s" % (node["name"], node["node_id"], fp))
        print("Daemon:      not running  ->  `cc-relay-hub node install` or `cc-relay-hub node start`")
        return 3
    rv = health["rendezvous"]
    print("Node:        %s (%s)  cluster %s  up %s" % (health["name"], health["node_id"], health["cluster_fingerprint"], _fmt_ago(time.time() - health["uptime_secs"]).replace(" ago", "")))
    print("Listening:   port %s  addrs %s" % (health["port"], ", ".join(health["addrs"]) or "-"))
    print("Rendezvous:  %s  %s%s" % (
        rv["url"], "connected" if rv["connected"] else "DISCONNECTED",
        ("  (card %s)" % _fmt_ago(rv["last_card_at"])) if rv.get("last_card_at") else "",
    ))
    if rv.get("last_error"):
        print("             last error: %s" % rv["last_error"])
    print("Beacon:      %s" % ("on (udp %s)" % node.get("beacon_port", 9131) if health["beacon"] else "off"))
    peers = health["peers"]
    print("Peers (%d):" % len(peers))
    if peers:
        print("  %-14s %-8s %-6s %-22s %-8s %s" % ("NAME", "STATUS", "PATH", "ADDR", "RTT", "AGENTS"))
        for peer in peers:
            rtt = ("%.0fms" % peer["rtt_ms"]) if peer.get("rtt_ms") else "-"
            print("  %-14s %-8s %-6s %-22s %-8s %s" % (
                peer["name"], "online" if peer["online"] else "offline", peer["path"], peer.get("addr") or "-", rtt,
                ", ".join(peer["agents"]) or "-",
            ))
    else:
        print("  none yet. On a machine already in the cluster run `cc-relay-hub node invite`, then `cc-relay-hub node join <string>` here.")
    print("Outbox pending: %d" % health["outbox_pending"])
    return 0


def cmd_node_invite(_args):
    node = node_identity.ensure_node(hub.HUB_DIR)
    secret = node_identity.ensure_secret(hub.HUB_DIR, node)
    invite = node_identity.encode_invite(secret, node.get("rendezvous_url") or node_identity.DEFAULT_RENDEZVOUS_URL)
    print(invite)
    print("")
    print("Tell the agent on the new computer (for example in Feishu):")
    print("  加入 cc-relay 集群：%s" % invite)
    print("It will run `cc-relay-hub node join <string>`. Anyone holding this string can join the cluster; share it privately.")
    return 0


def cmd_node_join(args):
    try:
        invite = node_identity.decode_invite(args.invite)
    except ValueError as exc:
        print("Error: %s" % exc)
        return 2
    node = node_identity.ensure_node(hub.HUB_DIR)
    existing = node_identity.load_secret(hub.HUB_DIR, node)
    if existing and existing != invite["secret"]:
        old_fp = node_identity.ClusterKeys(existing).fingerprint
        print("Replacing cluster %s with the invited cluster." % old_fp)
    node_identity.save_secret(hub.HUB_DIR, invite["secret"], node)
    if node.get("rendezvous_url") != invite["rendezvous_url"]:
        node["rendezvous_url"] = invite["rendezvous_url"]
        node_identity.save_node(hub.HUB_DIR, node)
    fp = node_identity.ClusterKeys(invite["secret"]).fingerprint
    print("Joined cluster %s (rendezvous %s)." % (fp, invite["rendezvous_url"]))
    health = _health()
    if health:
        try:
            hub.node_api("POST", "/api/v1/reload", {}, timeout=10.0)
            print("Running daemon reloaded; peers will appear in `cc-relay-hub list` within about a minute.")
        except RuntimeError as exc:
            print("Daemon reload failed (%s); restart it with `cc-relay-hub node restart`." % exc)
    else:
        print("Start the daemon with `cc-relay-hub node install` (login service) or `cc-relay-hub node start`.")
    return 0


def cmd_node_doctor(args):
    checks = []

    def add(level, name, detail, fix=None):
        checks.append({"level": level, "check": name, "detail": detail, "fix": fix})

    if sys.version_info < (3, 9):
        add("FAIL", "python", "Python %s" % platform.python_version(), "install Python 3.9+")
    else:
        add("OK", "python", "Python %s" % platform.python_version())
    node = node_identity.load_node(hub.HUB_DIR)
    if not node:
        add("FAIL", "node.json", "missing", "cc-relay-hub node install")
    else:
        add("OK", "node.json", "%s (%s)" % (node["name"], node["node_id"]))
    secret = node_identity.load_secret(hub.HUB_DIR, node) if node else None
    if not secret:
        add("FAIL", "cluster secret", "missing", "cc-relay-hub node join <invite> or node start")
    else:
        add("OK", "cluster secret", "fingerprint %s" % node_identity.ClusterKeys(secret).fingerprint)
    url = (node or {}).get("rendezvous_url") or node_identity.DEFAULT_RENDEZVOUS_URL
    try:
        rtt = NtfyRendezvous(url).check()
        add("OK", "rendezvous", "%s reachable (%.0f ms)" % (url, rtt))
    except (RendezvousError, OSError) as exc:
        add("FAIL", "rendezvous", "%s unreachable: %s" % (url, exc), "allow outbound HTTPS to it, or self-host: python3 relay_server.py and set node.json rendezvous_url")
    health = _health()
    if not health:
        add("FAIL", "daemon", "not running", "cc-relay-hub node install")
        port = int((node or {}).get("listen_port") or 9130)
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("0.0.0.0", port))
            add("OK", "port %d" % port, "free")
        except OSError as exc:
            add("WARN", "port %d" % port, "in use: %s" % exc, "change listen_port in node.json")
        finally:
            probe.close()
    else:
        add("OK", "daemon", "up %ss, %d peer(s), %d pending" % (health["uptime_secs"], len(health["peers"]), health["outbox_pending"]))
        rv = health["rendezvous"]
        if not rv["connected"]:
            add("WARN", "rendezvous stream", rv.get("last_error") or "not connected", "wait 60s; if it stays disconnected check the rendezvous URL")
        if not health["addrs"]:
            add("WARN", "addresses", "no LAN/WAN address detected; direct paths impossible, relay still works")
        for peer in health["peers"]:
            level = "OK" if peer["online"] else "WARN"
            add(level, "peer %s" % peer["name"], "%s via %s%s" % (
                "online" if peer["online"] else "offline (%s)" % _fmt_ago(time.time() - (peer.get("last_seen_secs") or 0)) if peer.get("last_seen_secs") is not None else "never seen",
                peer["path"], (" %s" % peer["addr"]) if peer.get("addr") else "",
            ))
    from noded import _broadcast_addrs
    sent = []
    last_error = None
    for target in _broadcast_addrs():
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(b"{}", (target, int((node or {}).get("beacon_port") or 9131)))
            sock.close()
            sent.append(target)
        except OSError as exc:
            last_error = exc
    if sent:
        add("OK", "udp beacon", "broadcast to %s" % ", ".join(sent))
    else:
        add("WARN", "udp beacon", "broadcast failed: %s (LAN auto-discovery off; rendezvous cards still find LAN peers)" % last_error)

    if getattr(args, "json", False):
        print(json.dumps(checks, indent=2, ensure_ascii=False))
    else:
        for item in checks:
            line = "%-4s %-18s %s" % (item["level"], item["check"], item["detail"])
            if item.get("fix") and item["level"] != "OK":
                line += "\n     fix: %s" % item["fix"]
            print(line)
    worst = max((("OK", "WARN", "FAIL").index(c["level"]) for c in checks), default=0)
    return {0: 0, 1: 0, 2: 3}[worst]


def cmd_node_discover(_args):
    health = _health()
    if not health:
        print("Error: node daemon is not running")
        return 3
    nodes = health.get("lan_discovered") or []
    if not nodes:
        print("No nodes from other clusters seen on the LAN.")
        return 0
    for item in nodes:
        print("  %-16s %s:%s  (node %s, seen %s)" % (item.get("name"), item.get("ip"), item.get("port"), item.get("node_id"), _fmt_ago(item.get("seen_at"))))
    print("To bring one into this cluster, run `cc-relay-hub node invite` here and `cc-relay-hub node join <string>` there.")
    return 0


# ---------------------------------------------------------------------------
# peer
# ---------------------------------------------------------------------------
def cmd_peer_list(args):
    health = _health()
    if health:
        peers = health["peers"]
    else:
        store = StateStore(str(hub.STATE_DB_PATH))
        peers = [{
            "node_id": p["node_id"], "name": p["name"], "online": False, "path": p.get("path") or "-",
            "addr": p.get("best_addr"), "rtt_ms": p.get("rtt_ms"), "addrs": p["addrs"],
            "agents": [a["name"] for a in store.list_remote_agents(p["node_id"])],
        } for p in store.list_peers()]
    if getattr(args, "json", False):
        print(json.dumps(peers, indent=2, ensure_ascii=False))
        return 0
    if not peers:
        print("No peers known.")
        return 0
    for peer in peers:
        print("  %-14s %-8s path=%-6s addr=%-22s agents=%s" % (
            peer["name"], "online" if peer["online"] else "offline", peer["path"], peer.get("addr") or "-", ", ".join(peer["agents"]) or "-",
        ))
    if not health:
        print("(daemon not running; showing cached data)")
    return 0


def cmd_peer_add(args):
    try:
        result = hub.node_api("POST", "/api/v1/peers/add", {"address": args.address, "name": args.name}, timeout=30.0)
    except (hub.NodeDaemonUnavailable, RuntimeError) as exc:
        print("Error: %s" % exc)
        return 1
    print("Peer %s (%s) added via %s, %.0f ms" % (result.get("name"), result.get("node_id"), result.get("path"), result.get("rtt_ms") or 0))
    if result.get("warning"):
        print("Warning: %s" % result["warning"])
    return 0


def cmd_peer_remove(args):
    try:
        result = hub.node_api("POST", "/api/v1/peers/remove", {"node": args.node}, timeout=10.0)
    except hub.NodeDaemonUnavailable:
        store = StateStore(str(hub.STATE_DB_PATH))
        peer = store.find_peer_by_name(args.node)
        if not peer:
            print("Error: unknown peer %s" % args.node)
            return 1
        store.remove_peer(peer["node_id"])
        result = {"removed": peer["node_id"]}
    except RuntimeError as exc:
        print("Error: %s" % exc)
        return 1
    print("Removed peer %s" % result.get("removed"))
    return 0


def run_node_command(args):
    if args.command == "node":
        handlers = {
            "start": cmd_node_start,
            "install": cmd_node_install,
            "uninstall": cmd_node_uninstall,
            "stop": cmd_node_stop,
            "restart": cmd_node_restart,
            "status": cmd_node_status,
            "invite": cmd_node_invite,
            "join": cmd_node_join,
            "doctor": cmd_node_doctor,
            "discover": cmd_node_discover,
        }
        handler = handlers.get(getattr(args, "node_command", None))
        if not handler:
            print("Usage: cc-relay-hub node <start|install|uninstall|stop|restart|status|invite|join|doctor|discover>")
            return 1
        return handler(args)
    handlers = {"list": cmd_peer_list, "add": cmd_peer_add, "remove": cmd_peer_remove}
    handler = handlers.get(getattr(args, "peer_command", None))
    if not handler:
        print("Usage: cc-relay-hub peer <list|add|remove>")
        return 1
    return handler(args)
