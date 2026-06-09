#!/usr/bin/env python3
"""Send images / video / files / text to the active Feishu (Lark) chat.

Why this exists: cc-connect's `cc-connect send --image` relies on a local unix
socket (`~/.cc-connect/run/api.sock`). That socket frequently gets unlinked while
the bridge keeps running, so `send` reports "socket not found" even though text
replies still flow. This helper bypasses the socket and talks to the Lark Open
API directly, so attachments always go through.

Usage:
  feishu-send.py --text "msg"
  feishu-send.py --image /abs/a.jpg [--image ...]
  feishu-send.py --video /abs/a.mp4 --thumb /abs/a.jpg     # inline-playable video
  feishu-send.py --file  /abs/report.pdf

Credentials & target are read at runtime — nothing is hardcoded:
  - app_id / app_secret  <- ~/.cc-connect/config.toml, project = $CC_PROJECT
  - chat_id              <- $CC_SESSION_KEY (format: feishu:<chat_id>:<open_id>)
                            or override with --chat <chat_id>

Pure Python stdlib, no third-party deps. Success prints "<kind> -> 0 success".
"""
import argparse, json, os, re, urllib.request

API = "https://open.feishu.cn/open-apis"
BOUNDARY = "----feishuBoundary7MA4YWxkTrZu0gW"


def http_json(url, data=None, headers=None, method="POST"):
    body = json.dumps(data).encode() if data is not None else None
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def multipart(fields, file_field, file_path):
    parts = []
    for name, val in fields.items():
        parts.append(f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode())
    fn = os.path.basename(file_path)
    parts.append((f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{fn}"\r\n'
                  f'Content-Type: application/octet-stream\r\n\r\n').encode())
    with open(file_path, "rb") as f:
        parts.append(f.read())
    parts.append(f"\r\n--{BOUNDARY}--\r\n".encode())
    return b"".join(parts)


def post_multipart(url, tok, payload):
    req = urllib.request.Request(url, data=payload, method="POST",
        headers={"Authorization": f"Bearer {tok}",
                 "Content-Type": f"multipart/form-data; boundary={BOUNDARY}"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def get_creds(config, project):
    txt = open(config, encoding="utf-8").read()
    for b in re.split(r"(?=\[\[projects\]\])", txt):
        if re.search(r'name\s*=\s*"%s"' % re.escape(project), b):
            a = re.search(r'app_id\s*=\s*"([^"]+)"', b)
            s = re.search(r'app_secret\s*=\s*"([^"]+)"', b)
            if a and s:
                return a.group(1), s.group(1)
    raise SystemExit(f"no feishu app creds for project {project} in {config}")


def token(app_id, app_secret):
    d = http_json(f"{API}/auth/v3/tenant_access_token/internal",
                  {"app_id": app_id, "app_secret": app_secret})
    t = d.get("tenant_access_token")
    if not t:
        raise SystemExit(f"token error: {d}")
    return t


def upload_image(tok, path):
    d = post_multipart(f"{API}/im/v1/images", tok,
                       multipart({"image_type": "message"}, "image", path))
    key = d.get("data", {}).get("image_key")
    if not key:
        raise SystemExit(f"image upload failed {path}: {d}")
    return key


def upload_file(tok, path, file_type):
    d = post_multipart(f"{API}/im/v1/files", tok,
                       multipart({"file_type": file_type, "file_name": os.path.basename(path)}, "file", path))
    key = d.get("data", {}).get("file_key")
    if not key:
        raise SystemExit(f"file upload failed {path}: {d}")
    return key


def send_msg(tok, chat, msg_type, content):
    d = http_json(f"{API}/im/v1/messages?receive_id_type=chat_id",
                  {"receive_id": chat, "msg_type": msg_type, "content": json.dumps(content)},
                  headers={"Authorization": f"Bearer {tok}"})
    return d.get("code"), d.get("msg")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text")
    ap.add_argument("--image", action="append", default=[])
    ap.add_argument("--video", action="append", default=[], help="mp4 path (inline-playable)")
    ap.add_argument("--thumb", action="append", default=[], help="thumbnail jpg, paired with --video by order")
    ap.add_argument("--file", action="append", default=[], help="generic file (download message)")
    ap.add_argument("--chat", help="override chat_id (default: from $CC_SESSION_KEY)")
    ap.add_argument("--project", default=os.environ.get("CC_PROJECT", "my-project"))
    ap.add_argument("--config", default=os.environ.get("CC_CONFIG", os.path.expanduser("~/.cc-connect/config.toml")))
    a = ap.parse_args()

    chat = a.chat
    if not chat:
        sk = os.environ.get("CC_SESSION_KEY", "")
        if sk.startswith("feishu:"):
            chat = sk.split(":")[1]
    if not chat:
        raise SystemExit("no chat_id (set $CC_SESSION_KEY or pass --chat)")

    app_id, app_secret = get_creds(a.config, a.project)
    tok = token(app_id, app_secret)

    if a.text:
        print("text ->", *send_msg(tok, chat, "text", {"text": a.text}))
    for p in a.image:
        print("image ->", *send_msg(tok, chat, "image", {"image_key": upload_image(tok, p)}), p)
    for i, v in enumerate(a.video):
        content = {"file_key": upload_file(tok, v, "mp4")}
        if i < len(a.thumb):
            content["image_key"] = upload_image(tok, a.thumb[i])
        print("video ->", *send_msg(tok, chat, "media", content), v)
    for p in a.file:
        print("file ->", *send_msg(tok, chat, "file", {"file_key": upload_file(tok, p, "stream")}), p)


if __name__ == "__main__":
    main()
