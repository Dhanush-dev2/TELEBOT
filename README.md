# Telegram File-Sharing Bot (Pyrogram)

A production-oriented Telegram bot that stores admin-uploaded videos/documents
in a private channel and gives users self-destructing download links.

- Admin sends a video/document → bot copies it to `STORAGE_CHANNEL` (no
  "forwarded from" tag).
- Bot generates a short deep link: `https://t.me/<BOT_USERNAME>?start=<id>`.
- VPLink shortens the link (best-effort; the direct link is always returned).
- Optional force-subscribe channel with **Join Channel** and **Try Again**
  buttons.
- Delivered file + warning are deleted after `AUTO_DELETE_SECONDS` (default
  600 = 10 minutes). Pending deletions survive restarts.
- Library posts: `/newpost` creates a thumbnail/caption post with one download
  button per quality in `LINKS_CHANNEL`, then links it from `LIBRARY_CHANNEL`.
- `/editpost <post_id>` replaces one quality file and updates the existing
  button without recreating the post.
- SQLite stores files, users, pending deletions and library-post metadata.
- `/stats`, `/broadcast`, `/newpost` and `/editpost` admin commands, with
  FloodWait-aware retries.

---

## 1. Requirements

- Python **3.11** (recommended; Pyrogram 2.x compatibility)
- A Telegram bot token and username
- Telegram API ID / API hash
- A private storage channel where the bot is an administrator
- Optional: a force-subscribe channel where the bot is an administrator
- Optional: `LINKS_CHANNEL` and `LIBRARY_CHANNEL` where the bot is an
  administrator (required for `/newpost` and `/editpost`)
- A VPLink API key

---

## 2. Create the Telegram pieces

### 2.1 Bot token and username

1. Open [@BotFather](https://t.me/BotFather) in Telegram.
2. Send `/newbot`, choose a name and a username ending in `bot`.
3. Copy the token (looks like `123456789:AA...`) into `BOT_TOKEN`.
4. Put the username **without `@`** into `BOT_USERNAME`.

### 2.2 API ID and API hash

1. Go to <https://my.telegram.org> and log in with your Telegram account.
2. Open **API development tools** and create an application.
3. Copy `api_id` and `api_hash` into `API_ID` and `API_HASH`.
4. These are account API credentials, **not** the bot token.

### 2.3 Storage channel

1. Create a new **private channel** in Telegram.
2. Add your bot as an **administrator** with permission to post messages.
3. Get the numeric channel ID:
   - Forward any message from the channel to [@userinfobot](https://t.me/userinfobot), or
   - Open the channel in Telegram Web; the URL contains `-100...`, or
   - Use any "channel ID" bot.
4. Set `STORAGE_CHANNEL=-100...` (include the `-100` prefix). A public
   `@username` also works, but numeric IDs are safer.

The bot copies uploaded files there. It does not need permission to delete
messages in the storage channel.

### 2.4 Force-subscribe channel (optional)

1. Create or choose a channel.
2. Add the bot as an **administrator** (required for `get_chat_member`). If you
   want the bot to export its own invite link, also grant **Invite Users**.
3. Set `FORCE_SUB_CHANNEL`:
   - Public: `@your_channel`
   - Private: `-100...`
4. For a private channel, set `FORCE_SUB_INVITE_LINK=https://t.me/+...` (create
   an invite link in the channel settings). If left blank, the bot tries to
   export a link itself, which can fail depending on permissions.
5. Leave `FORCE_SUB_CHANNEL` empty to disable the gate.

### 2.5 Links and library channels

These two channels are used by `/newpost`:

- `LINKS_CHANNEL` receives the thumbnail + caption with one download button
  per quality (for example `🔗 1080p`, `🔗 720p`).
- `LIBRARY_CHANNEL` is the public-facing channel. It receives the same
  thumbnail + caption with a single **📥 Download Links** button pointing to
  the `LINKS_CHANNEL` post.

1. Create or choose both channels.
2. Add the bot as an **administrator** in both channels with permission to
   **Post Messages**. Keep these rights if you want to use `/editpost` later.
3. Set the variables:
   - Public channel: `LINKS_CHANNEL=@your_links_channel`
   - Private channel: `LINKS_CHANNEL=-100...`
   - `LIBRARY_CHANNEL=@your_library_channel` (or a numeric `-100...` ID)
4. Both values are optional at startup, but `/newpost` and `/editpost` show a
   clear error until they are configured and the bot is an administrator.

Private `LINKS_CHANNEL` posts use Telegram's `https://t.me/c/...` link format;
public channels use `https://t.me/<username>/<message_id>`.

### 2.6 VPLink API key

1. Create an account at <https://vplink.in>.
2. Copy the API key into `VPLINK_API_KEY`.
3. The bot calls:
   `GET https://vplink.in/api?api=KEY&url=LONG_URL&format=json`
   and reads the `shortenedUrl` field.
4. If VPLink is down or returns an error, the bot logs it and still sends the
   direct Telegram link.

### 2.7 Admin user IDs

Get your numeric Telegram ID from [@userinfobot](https://t.me/userinfobot) and
put it in `ADMIN_IDS`. Multiple admins are comma-separated:

```env
ADMIN_IDS=123456789,987654321
```

---

## 3. Configure

Copy the example file and edit it:

```bash
cp .env.example .env
```

PowerShell:

```powershell
Copy-Item .env.example .env
notepad .env
```

Every required variable is validated at startup. If one is missing, the bot
prints a list of problems and exits before connecting to Telegram.

| Variable | Required | Default | Description |
|---|---|---|---|
| `BOT_TOKEN` | yes | — | BotFather token |
| `API_ID` | yes | — | API ID from my.telegram.org |
| `API_HASH` | yes | — | API hash from my.telegram.org |
| `BOT_USERNAME` | yes | — | Bot username without `@` |
| `STORAGE_CHANNEL` | yes | — | `-100...` or `@username` |
| `LINKS_CHANNEL` | no* | — | Channel for per-quality download buttons |
| `LIBRARY_CHANNEL` | no* | — | Public-facing channel linking to the links post |
| `ADMIN_IDS` | yes | — | Comma-separated numeric user IDs |
| `VPLINK_API_KEY` | yes | — | VPLink API key |
| `FORCE_SUB_CHANNEL` | no | — | Channel gate; empty disables it |
| `FORCE_SUB_INVITE_LINK` | no | — | Invite link for private gate |
| `AUTO_DELETE_SECONDS` | no | `600` | Delay before deletion |
| `DATA_DIR` | no | `data` | Session/DB directory |
| `DATABASE_PATH` | no | `data/bot.db` | Main SQLite database |
| `SCHEDULER_DB_PATH` | no | `data/scheduler.db` | APScheduler jobstore |
| `SESSION_NAME` | no | `telebot` | Pyrogram session filename |
| `LOG_LEVEL` | no | `INFO` | Logging verbosity |

`*` `LINKS_CHANNEL` and `LIBRARY_CHANNEL` are optional for normal file
delivery, but both are required for `/newpost` and `/editpost`.

Relative paths are resolved against the repository root, not the current
working directory.

---

## 4. Run locally

### Linux / macOS

```bash
cd /path/to/telebot
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
nano .env          # fill in the values
python bot.py
```

### Windows (PowerShell)

```powershell
cd G:\telebot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env       # fill in the values
python bot.py
```

If PowerShell blocks activation, run:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

The first successful start creates:

```
data/bot.db         # files, users, pending deletions, library posts
data/scheduler.db   # APScheduler persistent jobs
data/telebot.session
data/*.db-wal / *.db-shm
```

### Manual test

1. Send a video or document to the bot from an admin account.
2. Confirm the bot replies with a direct and a shortened link.
3. Open the direct link from a **different** Telegram account.
4. If force-sub is configured, confirm the Join/Try Again flow.
5. Confirm the file and warning arrive, then verify they disappear after
   `AUTO_DELETE_SECONDS` (use `AUTO_DELETE_SECONDS=60` while testing).
6. Run `/stats` and reply to a message with `/broadcast` to test admin tools.
7. With both library channels configured, run `/newpost`, send a photo, a
   multi-line caption, then one or more files captioned `1080p`, `720p`, etc.
   Send `/done` and verify both channel posts and the buttons.
8. Run `/editpost <post_id>`, send a replacement with the same quality label,
   and confirm the button URL in `LINKS_CHANNEL` changes while the
   `LIBRARY_CHANNEL` "Download Links" button still points to the same post.

There is no automated test suite. A fast syntax check is:

```bash
python -m compileall -q bot.py config.py database.py handlers utils
```

---

## 5. How it works

### Upload flow

1. Admin sends a video/document in private chat.
2. `handlers/admin.py` copies it with `copy_message` to `STORAGE_CHANNEL`.
3. The copied message ID is base62-encoded into a short unique ID.
4. `files.unique_id -> files.message_id` is saved in SQLite.
5. The bot builds `https://t.me/<BOT_USERNAME>?start=<unique_id>`, shortens it
   through VPLink, and sends both links to the admin.

### Delivery flow

1. Any user opens `/start <unique_id>`.
2. The user is recorded in `users` (all interactions are recorded, not just
   `/start`).
3. The bot looks up the ID in `files`; unknown IDs are rejected.
4. If `FORCE_SUB_CHANNEL` is set, membership is checked. Non-members get the
   Join/Try Again prompt; Try Again re-checks and delivers on success.
5. The file is copied from storage to the user, followed immediately by the
   warning message.
6. A deletion job is stored in `pending_deletions` and in the APScheduler
   SQLite jobstore.

### Restart safety

`utils/scheduler.py` uses `SQLAlchemyJobStore` at `SCHEDULER_DB_PATH`. On
startup, `reload_pending_jobs()` re-registers every row in
`pending_deletions`, so deletions still run if the scheduler database was lost
or a job was missed. Both databases live in `data/`.

### Library post flow (`/newpost`)

1. The admin runs `/newpost`. The bot checks that both library channels are
   configured and that it is an administrator with post rights in each one.
2. The admin sends a thumbnail photo, then the title/caption text (multi-line
   Markdown is supported), then any number of video/document files. Each file's
   caption is only its quality label, such as `1080p` or `720p`.
3. `/done` copies every quality file to `STORAGE_CHANNEL`, creates its bot deep
   link, and shortens it with VPLink (falling back to the direct link if VPLink
   fails).
4. The bot posts the thumbnail/caption to `LINKS_CHANNEL` with one URL button
   per quality, then posts the same content to `LIBRARY_CHANNEL` with a single
   **📥 Download Links** button pointing at the `LINKS_CHANNEL` message.
5. Post metadata is stored in the `library_posts` and
   `library_post_qualities` SQLite tables.

`/editpost <post_id>` starts a short edit session. Send a replacement
video/document whose caption matches an existing quality label. The bot copies
the new file to storage, regenerates/shortens the link, updates the database,
and calls `edit_message_reply_markup` to change that button in the existing
`LINKS_CHANNEL` message. The `LIBRARY_CHANNEL` button does not need to change.

### Admin commands

| Command | Description |
|---|---|
| `/stats` | Files stored, unique users who used `/start`, known users, pending deletions |
| `/broadcast` (reply) | Copy the replied message to every known user |
| `/broadcast <text>` | Send plain text to every known user |
| `/newpost` | Guided thumbnail + caption + quality-file library post |
| `/editpost <post_id>` | Replace one quality file and update its button URL |
| `/cancel` | Abort an active `/newpost` or `/editpost` draft |

Broadcasts send with a 50 ms delay and respect `FloodWait` by sleeping and
retrying. Users who blocked the bot are counted as failed and skipped.

---

## 6. Deploy with Docker

```bash
cp .env.example .env
nano .env
mkdir -p data
docker compose up -d --build
docker compose logs -f
```

Stop / restart:

```bash
docker compose restart
docker compose down
```

Notes:

- `./data` is mounted into `/app/data`; it holds the session and both SQLite
  databases. Keep it when recreating the container.
- The image runs as UID `1000`. On Linux, if you see permission errors:
  `sudo chown -R 1000:1000 data`
- Rebuild after code changes: `docker compose up -d --build`.
- Use only one bot container per `data` directory. Two instances sharing a
  Pyrogram session or SQLite database will conflict.

---

## 7. Deploy with systemd

The provided `telegrambot.service` assumes:

- Project directory: `/opt/telegrambot`
- Virtualenv: `/opt/telegrambot/.venv`
- User/group: `telegrambot`
- Environment file: `/opt/telegrambot/.env`

```bash
# 1. Create a service user
sudo useradd --system --create-home --shell /usr/sbin/nologin telegrambot

# 2. Copy the project (exclude local venv/data)
sudo mkdir -p /opt/telegrambot
sudo rsync -a --exclude .venv --exclude data --exclude .env ./ /opt/telegrambot/

# 3. Install dependencies
cd /opt/telegrambot
sudo python3.11 -m venv .venv
sudo .venv/bin/python -m pip install --upgrade pip
sudo .venv/bin/pip install -r requirements.txt

# 4. Configure
sudo cp .env.example .env
sudo nano .env
sudo mkdir -p /opt/telegrambot/data
sudo chown -R telegrambot:telegrambot /opt/telegrambot
sudo chmod 600 /opt/telegrambot/.env
sudo chmod 700 /opt/telegrambot/data

# 5. Install and start the service
sudo cp telegrambot.service /etc/systemd/system/telegrambot.service
sudo systemctl daemon-reload
sudo systemctl enable --now telegrambot

# 6. Check status / logs
sudo systemctl status telegrambot
sudo journalctl -u telegrambot -f
```

`EnvironmentFile` uses systemd syntax. Values containing spaces must be quoted;
`#` starts a comment. The same `.env` works for local runs, Docker, and systemd.

`KillSignal=SIGINT` gives the bot a graceful shutdown window (`TimeoutStopSec=20`).
Pending deletion jobs are persisted regardless.

---

## 8. Project structure

```
telebot/
├── bot.py                      # entry point, client startup, handler registration
├── config.py                   # .env loading + startup validation
├── database.py                 # SQLite schema and queries
├── handlers/
│   ├── __init__.py
│   ├── admin.py                # upload handling, /stats, /broadcast
│   ├── library.py              # /newpost and /editpost workflows
│   └── user.py                 # /start, force-sub gate, delivery, user logging
├── utils/
│   ├── __init__.py
│   ├── channels.py             # channel resolution + admin checks
│   ├── encoding.py             # base62 deep-link IDs
│   ├── force_sub.py            # membership check + invite link
│   ├── pyrogram_compat.py      # wider peer-ID bounds for new channels
│   ├── scheduler.py            # APScheduler + persistent auto-delete
│   ├── shortener.py            # VPLink API wrapper
│   ├── storage.py              # copy to storage + build/share links
│   └── telegram.py             # FloodWait-aware API wrappers
├── data/                       # created at runtime (gitignored)
├── .env.example
├── .gitignore
├── .dockerignore
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── telegrambot.service
├── AGENTS.md                   # guidance for AI coding agents
└── README.md
```

---

## 9. Troubleshooting

**`Cannot access STORAGE_CHANNEL=...`**

- Add the bot as a channel administrator.
- Use the numeric `-100...` ID (not the invite link or channel title).
- If using `@username`, the channel must be public.

**`/newpost` says library posting is not configured**

- Set both `LINKS_CHANNEL` and `LIBRARY_CHANNEL` in `.env`, then restart.
- Use a public `@username` or the numeric `-100...` channel ID.

**`/newpost` or `/editpost` says the bot is not an administrator**

- Add the bot as an administrator in both library channels.
- Grant **Post Messages** in both channels.
- For `/editpost`, the bot must still be able to edit the original
  `LINKS_CHANNEL` message; deleting that message makes the edit impossible.

**Post creation failed after uploading files**

- Check the caption uses valid Telegram Markdown and is at most 1024
  characters.
- Confirm both library channels still allow the bot to post.
- The bot removes partial channel posts when a later step fails, but files
  already copied to `STORAGE_CHANNEL` remain there and can be reused.

**Force-sub check fails or users stay stuck**

- The bot must be an administrator of the force-sub channel.
- For private channels, set `FORCE_SUB_INVITE_LINK`.
- Verify `FORCE_SUB_CHANNEL` is the channel ID/username, not an invite link.

**Short link could not be generated**

- Check `VPLINK_API_KEY`, the VPLink account balance/status, and server network
  access. The direct Telegram link is still valid and sent to the admin.

**`database is locked`**

- Run only one bot instance per `data` directory.
- Do not edit/delete `data/*.db` while the bot is running.
- WAL mode is enabled automatically; keep the `-wal`/`-shm` files with the DB.

**Bot does not respond to admins**

- `ADMIN_IDS` must contain numeric IDs, not usernames.
- Check the logs for a configuration error at startup.

**`AUTH_KEY_UNREGISTERED` / session problems**

- Stop the bot, delete `data/telebot.session`, and start it again. The bot
  re-authenticates with the token; the databases are unaffected.

**Deleting jobs never run after changing code**

- The APScheduler jobstore references `utils.scheduler._delete_messages_job` by
  name. Renaming/moving that function invalidates old jobs. Stop the bot,
  delete `data/scheduler.db`, and restart so
  `reload_pending_jobs()` rebuilds them from `pending_deletions`.

---

## 10. Security notes

- Never commit `.env`; it contains the bot token and API hash. It is already in
  `.gitignore`.
- Keep `ADMIN_IDS` minimal. Anyone in that list can broadcast to every user.
- Deep-link IDs are base62-encoded storage message IDs, so they are short but
  **not secret/unguessable**. Share links only with intended recipients. If you
  need unguessable links, replace the ID generator with a random token (and
  keep storing the mapping in `files`).
- `data/` contains the Pyrogram session and should be readable only by the bot
  user (`chmod 700`).
- Back up `data/` while the bot is stopped. Keep `bot.db`, `scheduler.db` and
  the session file together.
