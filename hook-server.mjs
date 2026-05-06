import http from "node:http";
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

const hubDir = path.dirname(new URL(import.meta.url).pathname);
const out = path.join(hubDir, "hook-events.jsonl");
const hubScript = path.join(hubDir, "hub.py");

function forwardToHub(payload) {
  const child = spawn("python3", [hubScript, "_on_hook"], {
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
  if (req.method !== "POST" || !req.url.startsWith("/cc-connect/hooks/")) {
    res.statusCode = 404;
    return res.end("not found");
  }

  let body = "";
  req.on("data", chunk => { body += chunk; });

  req.on("end", () => {
    try {
      const event = JSON.parse(body);
      fs.appendFileSync(out, JSON.stringify({
        received_at: new Date().toISOString(),
        path: req.url,
        hook_event: req.headers["x-hook-event"] || event.event || "",
        payload: event
      }) + "\n");
      forwardToHub(event);
      console.log(`[${new Date().toISOString()}] ${event.event || "unknown"} from ${event.project || "?"} session=${event.session_key || "?"}`);
      res.statusCode = 204;
      res.end();
    } catch (err) {
      console.error("parse error:", err.message);
      res.statusCode = 400;
      res.end(String(err));
    }
  });
}).listen(9120, "127.0.0.1", () => {
  console.log("hook server listening on http://127.0.0.1:9120");
});
