---
name: feishu-send
description: Use when an agent needs to send an image, video, or file attachment to the user's Feishu (Lark) chat and `cc-connect send --image` fails with "socket not found". Sends via the Lark Open API directly.
---

# feishu-send

Deliver attachments (image / video / file) to the active Feishu chat when
`cc-connect send --image|--file` is broken.

## When to use

`cc-connect send` delivers attachments through a local unix socket at
`~/.cc-connect/run/api.sock`. That socket is frequently unlinked while the bridge
keeps running — so `cc-connect send --image` reports `socket not found:
.../run/api.sock` even though your normal text replies still reach the user.

When that happens, **do not restart the bridge** (it would drop the live channel).
Use this method instead — it talks to the Lark Open API directly and needs no socket.

## How (one command)

```bash
python3 <repo>/bin/feishu-send.py --text "caption" \
  --image /abs/a.jpg --image /abs/b.jpg          # one or more images
python3 <repo>/bin/feishu-send.py --video /abs/clip.mp4 --thumb /abs/clip.jpg   # inline-playable video
python3 <repo>/bin/feishu-send.py --file  /abs/report.pdf                       # downloadable file
```

`<repo>` is this cc-relay-hub checkout (the script lives at `bin/feishu-send.py`).
Pure stdlib, no dependencies. On success each line prints `... -> 0 success`.

## How it resolves credentials & target (nothing hardcoded)

- **app_id / app_secret** — read from `~/.cc-connect/config.toml` for the project
  named by `$CC_PROJECT` (the cc-connect runtime sets this).
- **chat_id** — taken from `$CC_SESSION_KEY`, whose format is
  `feishu:<chat_id>:<open_id>`; the script uses the `<chat_id>` (`oc_...`) segment.
  Override with `--chat <chat_id>` if needed.

Because the bot credentials and the target ids are read at runtime from the local
config/env, the same script works for any agent/project without edits, and no
secrets or ids are stored in the script.

## Raw API flow (if you must reimplement)

1. `POST /open-apis/auth/v3/tenant_access_token/internal` with `{app_id, app_secret}`
   → `tenant_access_token`.
2. Upload the asset:
   - image → `POST /open-apis/im/v1/images` (multipart: `image_type=message`, `image=@file`) → `image_key`
   - file/video → `POST /open-apis/im/v1/files` (multipart: `file_type`, `file_name`, `file=@file`) → `file_key`
     (`file_type=mp4` for video, `stream` for generic files)
3. Send: `POST /open-apis/im/v1/messages?receive_id_type=chat_id` with
   `{receive_id:<chat_id>, msg_type, content:<json-string>}`:
   - image: `msg_type=image`, content `{"image_key": "..."}`
   - video: `msg_type=media`, content `{"file_key": "...", "image_key": "<thumb>"}`
   - file:  `msg_type=file`,  content `{"file_key": "..."}`
   - text:  `msg_type=text`,  content `{"text": "..."}`

All endpoints use `Authorization: Bearer <tenant_access_token>`.

## Troubleshooting

- Auth fails → re-check step 1 returns `code: 0` (bad app_secret or app not enabled).
- Image/file upload `code != 0` → the bot may lack `im:message` / `im:resource`
  scopes, or the bot is not a member of the target chat.
