#!/usr/bin/env python3
"""Minimal self-hostable rendezvous server speaking the ntfy subset cc-relay-hub uses.

    POST /<topic>                         publish a message (body = text)
    GET  /<t1,t2>/json?poll=1&since=<x>   fetch messages after cursor (id, unix ts, or "all")
    GET  /<t1,t2>/json?since=<x>          stream messages (ndjson, keepalive every 30s)

Messages are kept in memory for RETENTION_SECS. Run on any machine both nodes can
reach: `python3 relay_server.py --port 9140`, then set node.json rendezvous_url.
"""

import argparse
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

RETENTION_SECS = 12 * 3600
KEEPALIVE_SECS = 30.0


class Store(object):
    def __init__(self):
        self.lock = threading.Condition()
        self.messages = []  # ordered list of dicts
        self.seq = 0

    def publish(self, topic, text):
        with self.lock:
            self.seq += 1
            item = {
                "id": "%08d%s" % (self.seq, uuid.uuid4().hex[:4]),
                "seq": self.seq,
                "time": int(time.time()),
                "event": "message",
                "topic": topic,
                "message": text,
            }
            self.messages.append(item)
            cutoff = time.time() - RETENTION_SECS
            while self.messages and self.messages[0]["time"] < cutoff:
                self.messages.pop(0)
            self.lock.notify_all()
            return item

    def _since_seq(self, since):
        since = (since or "all").strip()
        if since == "all":
            return 0
        if since.isdigit() and len(since) >= 9:
            ts = int(since)
            for item in self.messages:
                if item["time"] >= ts:
                    return item["seq"] - 1
            return self.seq
        if since.endswith("s") and since[:-1].isdigit():
            ts = time.time() - int(since[:-1])
            for item in self.messages:
                if item["time"] >= ts:
                    return item["seq"] - 1
            return self.seq
        for item in self.messages:
            if item["id"] == since:
                return item["seq"]
        return 0

    def fetch(self, topics, since):
        with self.lock:
            after = self._since_seq(since)
            return [dict(m) for m in self.messages if m["seq"] > after and m["topic"] in topics]

    def wait(self, topics, after_seq, timeout):
        with self.lock:
            deadline = time.time() + timeout
            while True:
                fresh = [dict(m) for m in self.messages if m["seq"] > after_seq and m["topic"] in topics]
                if fresh:
                    return fresh
                remaining = deadline - time.time()
                if remaining <= 0:
                    return []
                self.lock.wait(remaining)


STORE = Store()


def _public(item):
    return {k: v for k, v in item.items() if k != "seq"}


class Handler(BaseHTTPRequestHandler):
    server_version = "cc-relay-mini/1"

    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            BaseHTTPRequestHandler.log_message(self, fmt, *args)

    def _topics(self, path):
        raw = path.strip("/").split("/")[0]
        return set(t for t in raw.split(",") if t)

    def do_POST(self):
        parsed = urlparse(self.path)
        topic = parsed.path.strip("/")
        if not topic or "/" in topic:
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        item = STORE.publish(topic, body)
        payload = json.dumps(_public(item)).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if len(parts) != 2 or parts[1] != "json":
            self.send_error(404)
            return
        topics = self._topics(parsed.path)
        query = parse_qs(parsed.query)
        since = (query.get("since") or ["all"])[0]
        poll = (query.get("poll") or ["0"])[0] == "1"
        if poll:
            items = STORE.fetch(topics, since)
            payload = "".join(json.dumps(_public(i)) + "\n" for i in items).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write((json.dumps({"id": "open", "time": int(time.time()), "event": "open", "topic": ",".join(topics)}) + "\n").encode("utf-8"))
            self.wfile.flush()
            with STORE.lock:
                after = STORE._since_seq(since)
            backlog = STORE.fetch(topics, since)
            for item in backlog:
                self.wfile.write((json.dumps(_public(item)) + "\n").encode("utf-8"))
                after = max(after, item["seq"])
            self.wfile.flush()
            while True:
                fresh = STORE.wait(topics, after, KEEPALIVE_SECS)
                if not fresh:
                    self.wfile.write((json.dumps({"id": "ka", "time": int(time.time()), "event": "keepalive", "topic": ",".join(topics)}) + "\n").encode("utf-8"))
                    self.wfile.flush()
                    continue
                for item in fresh:
                    self.wfile.write((json.dumps(_public(item)) + "\n").encode("utf-8"))
                    after = max(after, item["seq"])
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def serve(host="0.0.0.0", port=9140, verbose=False):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.verbose = verbose
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="cc-relay-hub mini rendezvous server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9140)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    server = serve(args.host, args.port, args.verbose)
    print("cc-relay-hub mini relay listening on http://%s:%d" % (args.host, args.port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
