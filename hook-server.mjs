import http from "node:http";
import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const hubDir = path.dirname(fileURLToPath(import.meta.url));
const out = process.env.HOOK_EVENTS_FILE || path.join(hubDir, "hook-events.jsonl");
const hubScript = path.join(hubDir, "hub.py");
const PORT = parseInt(process.env.HOOK_PORT || "9120", 10);

function parseCommand(value) {
  const text = (value || "").trim();
  if (!text) return null;
  if (text.startsWith('"')) {
    const end = text.indexOf('"', 1);
    if (end > 1) {
      const command = text.slice(1, end);
      const rest = text.slice(end + 1).trim();
      return { command, args: rest ? rest.split(/\s+/) : [] };
    }
  }
  const parts = text.split(/\s+/);
  return { command: parts[0], args: parts.slice(1) };
}

function pythonCandidates() {
  const candidates = [];
  const envPython = parseCommand(process.env.PYTHON_BIN || "");
  if (envPython) candidates.push(envPython);
  if (process.platform === "win32") {
    candidates.push({ command: "python", args: [] });
    candidates.push({ command: "py", args: ["-3"] });
    candidates.push({ command: "python3", args: [] });
  } else {
    candidates.push({ command: "python3", args: [] });
    candidates.push({ command: "python", args: [] });
  }
  return candidates;
}

function canRunPython(candidate) {
  const checks = [
    "import sys; assert sys.version_info >= (3, 9); import tomllib",
    "import sys; assert sys.version_info >= (3, 9); import tomli",
  ];
  return checks.some(code => {
    const result = spawnSync(candidate.command, [...candidate.args, "-c", code], {
      stdio: "ignore",
    });
    return result.status === 0;
  });
}

function resolvePython() {
  for (const candidate of pythonCandidates()) {
    if (canRunPython(candidate)) return candidate;
  }
  return null;
}

// ---------------------------------------------------------------------------
// In-memory event ring buffer + long-poll waiters
// ---------------------------------------------------------------------------
const MAX_BUFFER = 500;
const eventBuffer = [];          // newest at the end
const pendingWaiters = [];       // { resolve, timer }

function pushEvent(record) {
  eventBuffer.push(record);
  if (eventBuffer.length > MAX_BUFFER) {
    eventBuffer.splice(0, eventBuffer.length - MAX_BUFFER);
  }
  // Wake all pending long-poll waiters
  while (pendingWaiters.length) {
    const waiter = pendingWaiters.shift();
    clearTimeout(waiter.timer);
    waiter.resolve();
  }
}

function parseSince(value) {
  if (!value) return 0;
  const ts = Date.parse(value);
  return Number.isNaN(ts) ? 0 : ts;
}

function handleLongPoll(req, res) {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`);
  const sinceParam = url.searchParams.get("since") || "";
  const timeoutParam = parseInt(url.searchParams.get("timeout") || "30", 10);
  const timeoutSec = Math.min(Math.max(timeoutParam, 1), 60);
  const sinceMs = parseSince(sinceParam);

  // Check if there are already events newer than `since`
  const matching = eventBuffer.filter(e => e.received_ms > sinceMs);
  if (matching.length > 0) {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ events: matching }));
    return;
  }

  // No events yet -- wait for one (long-poll)
  let settled = false;
  const waiter = {};
  const promise = new Promise(resolve => {
    waiter.resolve = () => {
      if (settled) return;
      settled = true;
      resolve();
    };
    waiter.timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      // Remove from pendingWaiters to prevent leak
      const idx = pendingWaiters.indexOf(waiter);
      if (idx !== -1) pendingWaiters.splice(idx, 1);
      resolve();   // resolve with timeout (will return empty)
    }, timeoutSec * 1000);
  });

  req.on("close", () => {
    if (!settled) {
      settled = true;
      clearTimeout(waiter.timer);
      // Remove from pendingWaiters if still there
      const idx = pendingWaiters.indexOf(waiter);
      if (idx !== -1) pendingWaiters.splice(idx, 1);
    }
  });

  pendingWaiters.push(waiter);

  promise.then(() => {
    if (res.writableEnded) return;
    const fresh = eventBuffer.filter(e => e.received_ms > sinceMs);
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ events: fresh }));
  });
}

// ---------------------------------------------------------------------------
// Forward hook event to hub.py
// ---------------------------------------------------------------------------
function forwardToHub(payload) {
  const python = resolvePython();
  if (!python) {
    console.error("hub forward error: Python 3.9+ with tomllib or tomli was not found");
    return;
  }
  const child = spawn(python.command, [...python.args, hubScript, "_on_hook"], {
    stdio: ["pipe", "pipe", "pipe"],
  });

  child.stdin.write(JSON.stringify(payload));
  child.stdin.end();

  child.stdout.on("data", chunk => {
    const text = String(chunk).trim();
    if (text) {
      console.log(`[hub] ${text}`);
    }
  });

  child.stderr.on("data", chunk => {
    const text = String(chunk).trim();
    if (text) {
      console.error(`[hub] ${text}`);
    }
  });

  child.on("error", err => {
    console.error("hub forward error:", err.message);
  });

  child.on("exit", code => {
    if (code !== 0) {
      console.error(`hub forward exited with code ${code}`);
    }
  });
}

http.createServer((req, res) => {
  // --- Long-poll endpoint for event streaming ---
  if (req.method === "GET" && req.url.startsWith("/events/longpoll")) {
    handleLongPoll(req, res);
    return;
  }

  // --- Hook event receiver (cc-connect POST) ---
  if (req.method !== "POST" || !req.url.startsWith("/cc-connect/hooks/")) {
    res.statusCode = 404;
    return res.end("not found");
  }

  let body = "";
  req.on("data", chunk => { body += chunk; });

  req.on("end", () => {
    try {
      const event = JSON.parse(body);

      // Only process relay messages — skip non-relay messages (e.g. user's
      // direct chat with the agent) to avoid cross-contamination.
      const content = event.content || "";
      const isRelayReply = /\[cc-relay reply_to=/.test(content);
      if (!isRelayReply) {
        res.statusCode = 204;
        return res.end();
      }

      const now = new Date();
      const record = {
        received_at: now.toISOString(),
        received_ms: now.getTime(),
        path: req.url,
        hook_event: req.headers["x-hook-event"] || event.event || "",
        payload: event
      };
      fs.appendFileSync(out, JSON.stringify(record) + "\n");
      pushEvent(record);
      forwardToHub(event);
      console.log(`[${now.toISOString()}] ${event.event || "unknown"} from ${event.project || "?"} session=${event.session_key || "?"}`);
      res.statusCode = 204;
      res.end();
    } catch (err) {
      console.error("parse error:", err.message);
      res.statusCode = 400;
      res.end(String(err));
    }
  });
}).listen(PORT, "127.0.0.1", () => {
  console.log(`hook server listening on http://127.0.0.1:${PORT}`);
});
