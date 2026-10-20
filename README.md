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
- Turso (libSQL) stores files, users, pending deletions and library-post
  metadata, so data survives Render restarts and redeploys.
- A built-in aiohttp server answers `GET /` with `OK` on Render's `PORT` so an
  external uptime monitor can keep the free web service awake.
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
- A Turso account and database (free tier is enough; see section 3)

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

## 3. Set up Turso (database)

The bot no longer writes a local SQLite database. Files, users, pending
deletions and library-post metadata live in Turso (libSQL), so they survive
Render restarts, redeploys and free-tier disk wipes.

### 3.1 Install the Turso CLI

macOS / Linux:

```bash
curl -sSfL https://get.tur.so/install.sh | bash
```

Windows: the Turso CLI requires WSL. Open PowerShell, run `wsl`, then run these
commands inside the WSL shell:

```bash
curl -sSfL https://get.tur.so/install.sh | bash
exec bash -l
turso --version
cd /mnt/g/telebot          # WSL mount point for G:\telebot; adjust if needed
```

You can also create the database and token from the Turso dashboard instead of
the CLI.

### 3.2 Create the database and token

```bash
# Create an account, or log in if you already have one.
turso auth signup          # alternative: turso auth login
# In WSL/headless environments use: turso auth signup --headless

# Create a database and wait until it is ready.
turso db create telebot --wait

# Copy this output; it starts with libsql://
turso db show telebot --url

# Create a non-expiring token and copy its output.
turso db tokens create telebot --expiration never
```

If you prefer the dashboard: open **Databases** → select the database →
**Connect** to copy the `libsql://` URL, then use **Tokens** → **Create
Token** for the auth token.

Put the two values in `.env`:

```env
TURSO_DATABASE_URL=libsql://your-database-your-org.turso.io
TURSO_AUTH_TOKEN=eyJ...
```

The bot creates all tables automatically on its first successful startup.
Verify the connection (before or after the first run):

```bash
turso db shell telebot "SELECT 1"
```

**Migrating existing local data (optional):** stop every running bot instance
first so the local SQLite database is not being written. **Skip the plain
`turso db create telebot --wait` above** and create the Turso database from the
old `data/bot.db` instead:

```bash
turso db create telebot --from-file data/bot.db --wait
```

The old `data/scheduler.db` is no longer used and does not need to be imported.
If the local DB uses WAL mode and you want to be certain recent committed rows
are included, use the dump/load command below instead of `--from-file`. If you
already created an empty database, dump the local DB and load it:

```bash
# Linux / macOS / WSL; requires the sqlite3 CLI
sqlite3 data/bot.db .dump | turso db shell telebot
```

**Local-only test database:** for development without a Turso account, set
`TURSO_DATABASE_URL=file:data/dev.db` and leave `TURSO_AUTH_TOKEN` empty. This
is not durable on Render; use a real `libsql://` URL in production.

### 3.3 Scheduler persistence

APScheduler keeps its jobs in memory. The durable job queue is the
`pending_deletions` table in Turso: every auto-delete job is written there
first, and `reload_pending_jobs()` re-registers all unfinished rows at startup.
This gives the same restart safety as a remote jobstore without a second
database connection. See section 6 for details.

This is intentionally different from `SQLAlchemyJobStore`: the
`sqlalchemy-libsql` dialect is experimental and only supports Linux/macOS,
while `libsql-client` is pure Python and works for local Windows development.
The trade-off is that **only jobs represented in `pending_deletions` survive a
restart**. If you add another kind of APScheduler job later, persist its
parameters in Turso and replay it during startup too.

---

## 4. Configure

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
| `TURSO_DATABASE_URL` | yes | — | `libsql://...` URL from `turso db show` |
| `TURSO_AUTH_TOKEN` | yes* | — | Token from `turso db tokens create` |
| `FORCE_SUB_CHANNEL` | no | — | Channel gate; empty disables it |
| `FORCE_SUB_INVITE_LINK` | no | — | Invite link for private gate |
| `AUTO_DELETE_SECONDS` | no | `600` | Delay before deletion |
| `PORT` | no | `8080` | Health server port; Render sets this |
| `DATA_DIR` | no | `data` | Pyrogram session directory (ephemeral on Render) |
| `SESSION_NAME` | no | `telebot` | Pyrogram session filename |
| `LOG_LEVEL` | no | `INFO` | Logging verbosity |

`*` `LINKS_CHANNEL` and `LIBRARY_CHANNEL` are optional for normal file
delivery, but both are required for `/newpost` and `/editpost`.
`TURSO_AUTH_TOKEN` is required for remote `libsql://` URLs; it can be empty
only when using a local `file:` test database.

Relative paths are resolved against the repository root, not the current
working directory. There is no local database file: all bot data is in Turso.

---

## 5. Run locally

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

The first successful start creates only the disposable Pyrogram session:

```
data/telebot.session
```

All files, users, pending deletions and library-post metadata are created in
Turso. The bot also listens for the keep-alive endpoint locally:

```bash
curl http://127.0.0.1:8080/     # returns OK
```

PowerShell equivalent: `Invoke-WebRequest http://127.0.0.1:8080/`.

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

## 6. How it works

### Upload flow

1. Admin sends a video/document in private chat.
2. `handlers/admin.py` copies it with `copy_message` to `STORAGE_CHANNEL`.
3. The copied message ID is base62-encoded into a short unique ID.
4. `files.unique_id -> files.message_id` is saved in Turso.
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
6. A deletion row is written to `pending_deletions` and the job is scheduled
   in APScheduler's in-memory jobstore.

### Restart safety

`utils/scheduler.py` uses `MemoryJobStore`; the persistent queue is the
`pending_deletions` table in Turso. On startup, `reload_pending_jobs()`
re-registers every unfinished row, scheduling already-expired deletions one
second in the future. This makes auto-delete jobs survive Render restarts and
redeploys without a second database or a local scheduler file.

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
5. Post metadata is stored in the Turso `library_posts` and
   `library_post_qualities` tables.

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

## 7. Deploy on Render.com (free web service)

The repository includes `render.yaml`, a Render Blueprint that creates one free
Python web service. The bot runs Pyrogram and the aiohttp keep-alive server in
the same process; Render injects `PORT`, and the app binds `0.0.0.0:$PORT`.

### 7.1 Push the project to GitHub

Render deploys from a Git repository. If this project is not in GitHub yet:

```bash
git init
git add .
git commit -m "Prepare bot for Render and Turso"
git branch -M main
git remote add origin https://github.com/YOUR_NAME/telebot.git
git push -u origin main
```

Make sure `.env` is not committed (it is already in `.gitignore`).

### 7.2 Create the Render Blueprint

1. Complete section 3 first and keep `TURSO_DATABASE_URL` and
   `TURSO_AUTH_TOKEN` ready.
2. In the [Render dashboard](https://dashboard.render.com/), choose
   **New +** → **Blueprint**.
3. Connect GitHub and select this repository. Render reads `render.yaml`.
4. Review the service (runtime Python, plan **Free**, health check path `/`)
   and click **Apply**.
5. When Render prompts for environment variables, fill in:
   - `BOT_TOKEN`
   - `API_ID`
   - `API_HASH`
   - `BOT_USERNAME`
   - `ADMIN_IDS`
   - `STORAGE_CHANNEL`
   - `VPLINK_API_KEY`
   - `TURSO_DATABASE_URL`
   - `TURSO_AUTH_TOKEN`
   - optional: `LINKS_CHANNEL`, `LIBRARY_CHANNEL`, `FORCE_SUB_CHANNEL`,
     `FORCE_SUB_INVITE_LINK`
6. Click **Deploy**. `PYTHON_VERSION`, `DATA_DIR=/tmp/telebot-data`,
   `AUTO_DELETE_SECONDS`, `SESSION_NAME` and `LOG_LEVEL` are already defined in
   `render.yaml`.
7. Watch **Logs** until you see the HTTP server message, successful Turso
   schema creation, the Telegram login, and `Bot is running`.
8. Open `https://<your-service>.onrender.com/` in a browser. It must return
   `OK`.

Do **not** set `PORT` manually: Render provides it and the app reads it. Do not
add a persistent disk; the only local file is the disposable Pyrogram session.
Turso holds all durable data.

Optional variables left blank at deploy time are treated as unset by
`config.py`. You can add or change them later under **Environment** in the
Render service; changing an environment variable triggers a redeploy.

### 7.3 Keep the free instance awake with UptimeRobot

Render free web services spin down after about 15 minutes without traffic. A
5-minute HTTP monitor prevents that. This also exercises `GET /`, which returns
`OK`.

1. Create a free account at [UptimeRobot](https://uptimerobot.com/).
2. Click **Add New Monitor**.
3. Monitor type: **HTTP(s)**.
4. Friendly name: `telebot-render`.
5. URL: `https://<your-service>.onrender.com/` (keep the trailing `/`).
6. Monitoring interval: **5 minutes** (the free-plan minimum).
7. Select an alert contact, then click **Create Monitor**.
8. Confirm the monitor shows **Up**. In Render's logs you should see periodic
   requests to `/`.

The first request after Render has slept can take 30–60 seconds to wake the
service; the monitor may briefly report a timeout, then recover. Pings every
5 minutes keep it active afterwards.

### 7.4 Deploying updates

Push to the connected GitHub branch. With `autoDeployTrigger: commit`, Render
rebuilds and redeploys automatically. Pending auto-delete jobs are reloaded
from Turso on the new instance. The old `data/telebot.session` is disposable
and is recreated after each deploy.

---

## 8. Deploy with Docker

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

- `./data` is mounted into `/app/data`; it holds only the disposable Pyrogram
  session. Database state is in Turso.
- `docker-compose.yml` publishes `${PORT:-8080}` so the health endpoint is
  reachable from the host.
- The image runs as UID `1000`. On Linux, if you see permission errors:
  `sudo chown -R 1000:1000 data`
- Rebuild after code changes: `docker compose up -d --build`.
- Run only one bot container per Telegram bot token/session. Two instances
  polling the same bot will conflict, even though both use Turso.

---

## 9. Deploy with systemd

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
Pending deletion rows are in Turso and are reloaded on the next startup.
`/opt/telegrambot/data` only contains the disposable Pyrogram session.

---

## 10. Project structure

```
telebot/
├── bot.py                      # entry point, client startup, handler registration
├── config.py                   # .env loading + startup validation
├── database.py                 # Turso/libSQL schema and queries
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
│   ├── http_server.py          # aiohttp keep-alive endpoint for Render
│   ├── pyrogram_compat.py      # wider peer-ID bounds for new channels
│   ├── scheduler.py            # APScheduler + Turso-backed auto-delete queue
│   ├── shortener.py            # VPLink API wrapper
│   ├── storage.py              # copy to storage + build/share links
│   └── telegram.py             # FloodWait-aware API wrappers
├── data/                       # Pyrogram session at runtime (gitignored)
├── .env.example
├── .gitignore
├── .dockerignore
├── requirements.txt
├── render.yaml                 # Render free web-service blueprint
├── Dockerfile
├── docker-compose.yml
├── telegrambot.service
├── AGENTS.md                   # guidance for AI coding agents
└── README.md
```

---

## 11. Troubleshooting

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

**Turso configuration or connection errors**

- `TURSO_DATABASE_URL` must start with `libsql://` and come from
  `turso db show <db> --url`; an `https://` URL is not accepted by this client.
- `TURSO_AUTH_TOKEN` must come from
  `turso db tokens create <db> --expiration never`. Do not wrap it in quotes in
  Render's environment settings.
- Verify the database and token independently:
  `turso db shell telebot "SELECT 1"`.
- If a token was revoked/expired or the database was deleted, create a new token
  (or database), update the env vars, and redeploy/restart.
- The startup log includes the Turso URL (never the token) so you can confirm
  which database the service is using.

**Render deploy fails or the service is not live**

- Keep `healthCheckPath` set to `/`; `GET /` must return `200 OK`.
- Do not set `PORT` manually. Render injects it, and `config.py` validates it.
- Check **Logs** for missing env vars, Turso errors, or Telegram channel errors.
- Optional variables left empty are treated as unset, not as errors.

**Render free service keeps going to sleep**

- Create the UptimeRobot HTTP(s) monitor from section 7.3 with a 5-minute
  interval and the exact URL `https://<service>.onrender.com/`.
- Check the monitor is **Up** and that Render logs show the periodic `/`
  requests.
- A sleeping service may take 30–60 seconds to answer the first ping.

**Bot does not respond to admins**

- `ADMIN_IDS` must contain numeric IDs, not usernames.
- Check the logs for a configuration error at startup.

**`AUTH_KEY_UNREGISTERED` / session problems**

- Stop the bot, delete `data/telebot.session` (or redeploy on Render), and start
  it again. The bot re-authenticates with the token; Turso data is unaffected.

**Auto-delete jobs are delayed after a restart**

- APScheduler uses an in-memory jobstore, so jobs are rebuilt from
  `pending_deletions` on startup. Check that the log says
  `Reloaded N pending auto-delete job(s)`.
- Rows whose deadline passed while the bot was offline run about one second
  after startup.
- There is no `data/scheduler.db` to delete or migrate anymore.

---

## 12. Security notes

- Never commit `.env`; it contains the bot token, API hash and Turso auth token.
  It is already in `.gitignore`.
- Keep `ADMIN_IDS` minimal. Anyone in that list can broadcast to every user.
- Deep-link IDs are base62-encoded storage message IDs, so they are short but
  **not secret/unguessable**. Share links only with intended recipients. If you
  need unguessable links, replace the ID generator with a random token (and
  keep storing the mapping in `files`).
- `data/` contains only the Pyrogram session; make it readable only by the bot
  user (`chmod 700`). Deleting it only forces a new Telegram login.
- Back up Turso with
  `turso db shell telebot .dump > telebot-backup.sql`, or use Turso's dashboard
  backup/point-in-time features. The local `data/` directory is not a database
  backup.
- Treat `TURSO_AUTH_TOKEN` as a secret in Render. If it leaks, revoke it in
  Turso and create a replacement token.
- `render.yaml` uses `sync: false` for secrets, so values are entered in the
  Render dashboard rather than committed to Git.
