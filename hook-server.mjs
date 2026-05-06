import http from "node:http";
import fs from "node:fs";
import path from "node:path";

const out = path.join(path.dirname(new URL(import.meta.url).pathname), "hook-events.jsonl");

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
