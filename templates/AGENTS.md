# cc-relay-hub Agent Context

You are connected to **cc-relay-hub**, a local multi-agent message router.
Use it to discover peers, check health, and delegate work to other agents.

## Discovery

```bash
cc-relay-hub list --format json     # discover all agents
cc-relay-hub info <agent>           # check agent health
cc-relay-hub groups                 # list groups and members
```

## Sending Messages

```bash
cc-relay-hub send <agent> "task description"
Get-Content task.md -Raw | cc-relay-hub send <agent> --stdin
cc-relay-hub send <agent> "status update" --no-reply
```

- For `cc_connect` agents, delivers via local webhook HTTP POST.
- For `cdp` agents such as Antigravity, delivers through the local Chrome DevTools Protocol session into the IDE agent chat.
- Always check `info <agent>` before sending.
- Before sending, decide whether the message needs a reply.
- If a reply is needed, use `send` without `--wait`; the target replies with `[cc-relay reply_to=...]`, and the hook server forwards that reply back to the origin session.
- If no reply is needed, use `send --no-reply`; the target must not answer only to acknowledge it.
- Do not use `cc-relay-hub watch`, `watch --loop`, shell polling, or `send --wait` to wait for private-message replies unless the user explicitly asks for a synchronous diagnostic wait.
- Do not send a new relay message merely to answer a relay reply; avoid reply-to-reply loops unless the user explicitly requests another round.
- If `info` shows `Provider: cdp`, still use `cc-relay-hub send <agent> "task"`; do not switch to `cc-connect relay`.
- For multiline or long tasks, use `--stdin` or `--message-file`; on Windows, do not pass a PowerShell multiline variable as positional `"task"`.

## CDP IDE Agents

CDP-backed agents are IDE windows controlled through localhost CDP. Antigravity commonly appears as `antigravity-ide`.

```bash
cc-relay-hub info antigravity-ide
cc-relay-hub cdp status antigravity-ide
cc-relay-hub send antigravity-ide "task description"
```

Use diagnostics only when needed:

```bash
cc-relay-hub cdp probe antigravity-ide
cc-relay-hub cdp heal antigravity-ide
cc-relay-hub cdp models antigravity-ide
cc-relay-hub cdp screenshot antigravity-ide --path /tmp/antigravity.png
```

- `Last Seen: never` is normal for CDP agents because replies are read from the IDE DOM, not from the hook server.
- Use `send --wait` only when the user explicitly asks for a synchronous diagnostic wait. If it times out, run `cdp status`, `cdp probe`, and `cdp screenshot` before retrying.
- Keep CDP ports bound to `127.0.0.1`; never expose the debugging port to a network.

## Delivering Images/Files to the End User (Feishu)

`cc-connect send --image` pushes by **chat_id** only. If that chat_id is stale/invalid
for the bot, Feishu rejects every push (image *and* text) with:

```
code=230002  msg=Bot/User can NOT be out of the chat
```

Replies still reach the user because replies go by `message_id` (no membership check),
so this failure is silent until you try to push an attachment.

**Robust fallback — upload + send by `open_id`** (same mechanism Feishu's own API uses;
bypasses chat_id entirely). Credentials and the target `open_id` are read live from
`~/.cc-connect/config.toml`, so nothing secret is hardcoded here:

```bash
PROJECT="${CC_PROJECT:-my-project}" IMG="/absolute/path/to/image.png" python3 - <<'PY'
import json, os, re, uuid, mimetypes, urllib.request, urllib.error
proj = os.environ.get("PROJECT"); img = os.environ["IMG"]
cfg  = open(os.path.expanduser("~/.cc-connect/config.toml")).read()
blk  = next((b for b in re.split(r'(?m)^\[\[projects\]\]\s*$', cfg)
             if re.search(r'name\s*=\s*"%s"' % re.escape(proj), b)), cfg)
app_id     = re.search(r'app_id\s*=\s*"([^"]+)"', blk).group(1)
app_secret = re.search(r'app_secret\s*=\s*"([^"]+)"', blk).group(1)
open_id    = re.search(r'(ou_[a-z0-9]+)', blk).group(1)   # allow_from / admin_from

def post(url, payload, hdr=None):
    req = urllib.request.Request(url, json.dumps(payload).encode(),
            {"Content-Type":"application/json", **(hdr or {})}, method="POST")
    try: return json.load(urllib.request.urlopen(req, timeout=20))
    except urllib.error.HTTPError as e: return json.load(e)

tat = post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
           {"app_id":app_id,"app_secret":app_secret})["tenant_access_token"]
b = "----"+uuid.uuid4().hex
body  = f'--{b}\r\nContent-Disposition: form-data; name="image_type"\r\n\r\nmessage\r\n'.encode()
body += (f'--{b}\r\nContent-Disposition: form-data; name="image"; filename="{os.path.basename(img)}"\r\n'
         f'Content-Type: {mimetypes.guess_type(img)[0] or "application/octet-stream"}\r\n\r\n').encode()
body += open(img,"rb").read() + f"\r\n--{b}--\r\n".encode()
r = urllib.request.Request("https://open.feishu.cn/open-apis/im/v1/images", body,
        {"Authorization":f"Bearer {tat}","Content-Type":f"multipart/form-data; boundary={b}"}, method="POST")
try: up = json.load(urllib.request.urlopen(r, timeout=30))
except urllib.error.HTTPError as e: up = json.load(e)
key = up["data"]["image_key"]
res = post("https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
           {"receive_id":open_id,"msg_type":"image","content":json.dumps({"image_key":key})},
           {"Authorization":f"Bearer {tat}"})
print("sent:", res.get("code"), res.get("msg"), (res.get("data") or {}).get("message_id"))
PY
```

- Success prints `sent: 0 success <message_id>`.
- Try `cc-connect send --image` first; use this fallback when it returns `230002`.
- For non-image files: upload via `POST /open-apis/im/v1/files` (multipart `file_type` +
  `file`) to get a `file_key`, then send with `msg_type:"file"`, `content:{"file_key":...}`.

## Relay (Agent-to-Agent)

```bash
cc-relay-hub relay <from> <to> "task" --timeout 120
```

- Sends from one agent to another within the same group.
- Always waits for reply.
- Do not use this command for routine private messages from an agent conversation; prefer asynchronous `send`.

## Relay Protocol

When you receive a message containing `[cc-relay request_id=...]`:
1. Read the task after the marker.
2. Start your final response with: `[cc-relay reply_to=<same_id>]`
3. Put your answer after that marker line.
4. Do not use this marker for any other purpose.
5. Never answer `NO_REPLY` or an empty response to a cc-relay-hub request.
6. If the task says "only reply X", still put `X` after the required reply marker.

## Groups

Agents are organized into groups. Same-group agents can relay to each other.
Check your group: `cc-relay-hub list --format json`

## Rules

- Never hardcode agent names. Discover with `cc-relay-hub list`.
- Do not send messages across groups. Treat ungrouped agents as separate from named groups; exact agent names do not bypass group isolation.
- Never use shell polling loops (`tail -f`, `while true`, `sleep`) or long-running listeners to wait for replies.
- Use plain `cc-relay-hub send` for request/reply; use `--no-reply` for notices; use `--wait` only when explicitly requested.
- Check agent health before sending work.

## Agent Name Resolution

When you use `send`, `info`, or `relay`, the agent name is resolved as:
1. **Exact match** — `send codex-bot` finds `codex-bot` directly.
2. **Fuzzy match** — `send codex` matches agents with "codex" in name or type.
3. **Same-group preference** — among fuzzy matches, the agent in your group is preferred. You are identified by the `CC_PROJECT` environment variable.

If multiple agents match and you're unsure, use `cc-relay-hub list` to see exact names.
