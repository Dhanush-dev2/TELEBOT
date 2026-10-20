# AGENTS.md

Telegram file-sharing bot: Pyrogram 2 (async), SQLite, APScheduler, Python 3.11.

## Commands

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # config.py exits with a list of problems if invalid
python bot.py
python -m compileall -q bot.py config.py database.py handlers utils   # only automated check
```

No test suite. Docker: `docker compose up -d --build`, then `docker compose logs -f`.
Full setup, deployment and troubleshooting steps are in `README.md`.

## Architecture notes

- `config.py` validates the environment on import and builds the immutable `settings` object. Relative paths resolve from the repo root, not the CWD. Don't read env vars elsewhere.
- Handlers are registered explicitly in `bot.py` through `admin.register(app)`, `library.register(app)` / `user.register(app)`; the code does not use `@Client.on_message` decorators. Add new handlers in those `register()` functions.
- `/newpost` and `/editpost` keep per-admin drafts in memory (`handlers/library.py`); created post metadata lives in `library_posts` / `library_post_qualities`.
- `utils/pyrogram_compat.py` widens Pyrogram's peer-ID bounds for new `-100...` channels. It must be imported before handlers that call channel APIs (`bot.py` and `utils/channels.py` do this).
- Deep-link IDs are `base62(storage_message_id)` (`utils/encoding.py`) stored as `files.unique_id`. Changing the encoding breaks every existing link. They are short but guessable by design.
- Two SQLite files: `data/bot.db` (files, users, pending_deletions) and `data/scheduler.db` (APScheduler SQLAlchemy jobstore). `pending_deletions` is the source of truth; `reload_pending_jobs()` re-registers rows on startup.
- APScheduler persists `utils.scheduler._delete_messages_job` by reference. Renaming/moving it breaks queued jobs; clear `data/scheduler.db` after such a change.
- Telegram sends go through `utils/telegram.py` for FloodWait retries; keep new bulk sends on those wrappers.
- Force-sub membership checks and invite export require the bot to be an admin in `FORCE_SUB_CHANNEL`; private channels should set `FORCE_SUB_INVITE_LINK`.
- Docker runs as UID 1000 and persists only `/app/data` (session + DBs); keep `./data` writable by that UID.

## Behavior that is easy to break

- `/start` delivery must copy the file, immediately send the warning, then schedule deletion of both message IDs in one `pending_deletions` row.
- Broadcast text is sent with HTML parse mode disabled; media broadcasts use `copy_message`.
- User logging runs in handler group `-1`; moving it to group 0 would compete with the command/upload handlers.
