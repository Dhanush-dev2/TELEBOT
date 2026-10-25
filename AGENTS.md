# AGENTS.md

Telegram file-sharing bot: Pyrogram 2 (async), Turso/libSQL, APScheduler, aiohttp, Python 3.11.

## Commands

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # config.py exits with a list of problems if invalid
python bot.py
python -m compileall -q bot.py config.py database.py handlers utils tests
python -m unittest discover -s tests -v
```

Docker: `docker compose up -d --build`, then `docker compose logs -f`.
Full setup, deployment and troubleshooting steps are in `README.md`.

## Architecture notes

- `config.py` validates the environment on import and builds the immutable `settings` object. Relative paths resolve from the repo root, not the CWD. Don't read env vars elsewhere. `TURSO_DATABASE_URL` is required; `libsql://`, `wss://`, and `https://` remote forms are accepted, and `TURSO_AUTH_TOKEN` is required for all of them (only `file:` test databases may omit it). `PORT` defaults to 8080 and Render injects it.
- Handlers are registered explicitly in `bot.py` through `admin.register(app)`, `library.register(app)` / `user.register(app)`; the code does not use `@Client.on_message` decorators. Add new handlers in those `register()` functions.
- `/newseries`, `/addepisode`, `/editepisode`, `/newpost` and `/editpost` keep per-admin drafts in memory (`handlers/library.py`); `/migrateposts` keeps one migration draft per admin. `/editepisode` supports bare `/editepisode` (paginated series picker with `settings.library_channels` category filters), `/editepisode <series_id>` (episode picker) and `/editepisode <series_id> <episode_number_or_label>` (direct selection by 1-based position, database ID or current label; ambiguous labels show the picker). It stages label/link changes until `/done`, then updates `episodes` / `episode_links` and edits the existing Links Channel keyboard; a failed Telegram edit restores the previous DB values. Reject empty labels, migration placeholder text and labels equal to the series title. Series metadata lives in `series` / `episodes` / `episode_links`; legacy post metadata, including the category and `migrated` flag, lives in `library_posts` / `library_post_qualities`. `LIBRARY_CHANNELS` is parsed by `utils/categories.py` into `settings.library_channels`; startup validates each category independently in `bot.py` through `utils/channels.py`. The legacy `LIBRARY_CHANNEL` becomes category `default`. `/editpost` remains legacy-only and must not resolve series IDs because the IDs can collide.
- Series Links Channel messages store one keyboard row per episode (`<episode label> · <quality label>`). `/addepisode`, `/editepisode` and `/migrateposts` edit that message with `edit_message_reply_markup`; they never repost the Library Channel message. Enforce `MAX_LINKS_BUTTONS` (90) before appending and roll back the new `episodes` row if the Telegram edit fails. `/migrateposts` defaults an appended episode to `Episode N`; only an explicitly edited, valid label overrides it (never the old post title or prompt text).
- `library_posts.migrated` is backfilled with `ALTER TABLE` on startup. `/migrateposts` only selects `migrated = 0`; never reset the flag when an admin declines deleting the old library message.
- `utils/pyrogram_compat.py` widens Pyrogram's peer-ID bounds for new `-100...` channels. It must be imported before handlers that call channel APIs (`bot.py` and `utils/channels.py` do this).
- Deep-link IDs are `base62(storage_message_id)` (`utils/encoding.py`) stored as `files.unique_id`. Changing the encoding breaks every existing link. They are short but guessable by design.
- Turso/libSQL is the only durable store (`database.py`, one shared `libsql-client` created by `init_db()`). Remote URLs are normalized to HTTPS because current Turso endpoints reject Hrana WebSocket upgrades; multi-statement writes use atomic HTTP batches. Tables are created idempotently at startup. There is no local database file; `data/` holds only the disposable Pyrogram session.
- APScheduler uses `MemoryJobStore`; `pending_deletions` in Turso is the source of truth. `reload_pending_jobs()` re-registers every unfinished row on startup, including expired ones. Do not reintroduce a local SQLAlchemy jobstore or `data/scheduler.db`.
- `utils/http_server.py` runs an aiohttp keep-alive server on `0.0.0.0:$PORT`; `GET /` returns `OK`. It starts before Turso/Telegram startup so Render sees an open port.
- Telegram sends go through `utils/telegram.py` for FloodWait retries; keep new bulk sends on those wrappers.
- Force-sub membership checks and invite export require the bot to be an admin in `FORCE_SUB_CHANNEL`; private channels should set `FORCE_SUB_INVITE_LINK`.
- Docker runs as UID 1000 and persists only `/app/data` (session); keep `./data` writable by that UID. `render.yaml` defines the Render free web service, and secrets use `sync: false`.

## Behavior that is easy to break

- `/start` delivery must copy the file, immediately send the warning, then write one durable `pending_deletions` row and register the in-memory APScheduler job.
- Broadcast text is sent with HTML parse mode disabled; media broadcasts use `copy_message`.
- User logging runs in handler group `-1`; moving it to group 0 would compete with the command/upload handlers.
- Admin callback handlers share `ADMIN_CALLBACK`, so each data-prefix owner is registered in its own handler group. Pyrogram stops after the first matching handler in a group. `/editepisode` uses the `edep:` prefix and its handler stays in group `-6`; do not merge it with the category, series, migration or `/deletepost` callback handlers.
