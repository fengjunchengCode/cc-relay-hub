"""Rendezvous client speaking the ntfy pub/sub protocol (public ntfy.sh or self-hosted).

Only two operations are needed:
  publish(topic, text)                 POST {url}/{topic}
  poll(topics, since, timeout)         GET  {url}/{t1,t2}/json?poll=1&since=...
  stream(topics, since, on_message)    GET  {url}/{t1,t2}/json?since=...   (long-lived)
"""

import json
import socket
import threading
import time
import urllib.error
import urllib.request

USER_AGENT = "cc-relay-hub-node/1"
STREAM_SOCKET_TIMEOUT = 75.0   # ntfy keepalives arrive every ~45s


class RendezvousError(Exception):
    pass


class NtfyRendezvous(object):
    def __init__(self, base_url, timeout=15.0):
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout
        if not self.base_url:
            raise ValueError("rendezvous url is empty")

    def _topics_path(self, topics):
        if isinstance(topics, str):
            topics = [topics]
        return ",".join(topics)

    def publish(self, topic, text):
        data = text.encode("utf-8") if isinstance(text, str) else text
        request = urllib.request.Request(
            "%s/%s" % (self.base_url, topic),
            data=data,
            headers={"Content-Type": "text/plain", "User-Agent": USER_AGENT, "Cache": "no"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", errors="replace").strip()
            raise RendezvousError("publish HTTP %s: %s" % (err.code, detail[:200]))
        except (urllib.error.URLError, socket.timeout, OSError) as err:
            raise RendezvousError("publish failed: %s" % err)
        try:
            return json.loads(body) if body.strip() else {}
        except ValueError:
            return {}

    def poll(self, topics, since="all", timeout=None):
        url = "%s/%s/json?poll=1&since=%s" % (self.base_url, self._topics_path(topics), since or "all")
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as err:
            raise RendezvousError("poll HTTP %s" % err.code)
        except (urllib.error.URLError, socket.timeout, OSError) as err:
            raise RendezvousError("poll failed: %s" % err)
        messages = []
        for line in body.splitlines():
            event = _parse_event(line)
            if event:
                messages.append(event)
        return messages

    def stream(self, topics, since, on_message, stop_event, on_connect=None, on_error=None):
        """Blocking long-poll loop. Reconnects with backoff until stop_event is set."""
        backoff = 2.0
        cursor = since or "all"
        while not stop_event.is_set():
            url = "%s/%s/json?since=%s" % (self.base_url, self._topics_path(topics), cursor)
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=STREAM_SOCKET_TIMEOUT) as response:
                    if on_connect:
                        on_connect()
                    backoff = 2.0
                    while not stop_event.is_set():
                        line = response.readline()
                        if not line:
                            break
                        event = _parse_event(line.decode("utf-8", errors="replace"))
                        if not event:
                            continue
                        cursor = event["id"]
                        on_message(event)
            except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, OSError, ValueError) as err:
                if on_error:
                    on_error(err)
                stop_event.wait(backoff)
                backoff = min(backoff * 2, 60.0)
        return cursor

    def check(self):
        """Connectivity probe: publish nothing, just fetch an empty poll on a throwaway topic."""
        started = time.time()
        self.poll("ccrelayprobe", since="1s", timeout=10.0)
        return (time.time() - started) * 1000.0


def _parse_event(line):
    line = (line or "").strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict) or event.get("event") != "message":
        return None
    return {
        "id": event.get("id") or "",
        "time": event.get("time") or 0,
        "topic": event.get("topic") or "",
        "message": event.get("message") or "",
    }


def build_rendezvous(url):
    return NtfyRendezvous(url)


class StreamThread(threading.Thread):
    def __init__(self, rendezvous, topics, since, on_message, on_error=None, name="rendezvous-stream"):
        super(StreamThread, self).__init__(name=name, daemon=True)
        self.rendezvous = rendezvous
        self.topics = topics
        self.since = since
        self.on_message = on_message
        self.on_error = on_error
        self.stop_event = threading.Event()
        self.connected_at = None
        self.last_error = None

    def _on_connect(self):
        self.connected_at = time.time()
        self.last_error = None

    def _on_error(self, err):
        self.connected_at = None
        self.last_error = str(err)
        if self.on_error:
            self.on_error(err)

    def run(self):
        self.rendezvous.stream(
            self.topics, self.since, self.on_message, self.stop_event,
            on_connect=self._on_connect, on_error=self._on_error,
        )

    def stop(self):
        self.stop_event.set()
