"""Persistent auto-delete scheduling with APScheduler.

The scheduler uses an SQLAlchemy-backed SQLite jobstore so jobs survive
restarts. The ``pending_deletions`` table in the main database is the source of
truth used to re-register jobs on startup.

IMPORTANT: ``_delete_messages_job`` is persisted by reference in the jobstore.
Do not rename or move it without migrating/clearing the scheduler database.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from pyrogram import Client
from pyrogram.errors import FloodWait

import database
from config import settings

logger = logging.getLogger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None
_client: Optional[Client] = None

JOB_PREFIX = "auto-delete:"
MAX_DELETE_RETRIES = 3


def init_scheduler(client: Client) -> AsyncIOScheduler:
    """Create and start the scheduler. Must run inside the asyncio loop."""
    global _scheduler, _client
    _client = client

    jobstores = {
        "default": SQLAlchemyJobStore(
            url=f"sqlite:///{settings.scheduler_db_path.as_posix()}",
            tablename="apscheduler_jobs",
        )
    }
    _scheduler = AsyncIOScheduler(
        jobstores=jobstores,
        timezone=timezone.utc,
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 300,
        },
    )
    _scheduler.start()
    logger.info("APScheduler started (jobstore=%s)", settings.scheduler_db_path)
    return _scheduler


def shutdown_scheduler() -> None:
    """Stop the scheduler without removing persisted jobs."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped")
    _scheduler = None


def _schedule_job(
    row_id: int,
    chat_id: int,
    message_ids: list[int],
    run_at: datetime,
) -> None:
    if _scheduler is None:
        raise RuntimeError("Scheduler has not been initialised")

    _scheduler.add_job(
        _delete_messages_job,
        trigger=DateTrigger(run_date=run_at),
        args=[row_id, chat_id, message_ids],
        id=f"{JOB_PREFIX}{row_id}",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=300,
    )


async def schedule_deletion(
    chat_id: int,
    file_message_id: int,
    warning_message_id: Optional[int],
    delete_at: float,
) -> int:
    """Persist and schedule deletion of the file and warning messages."""
    row_id = await database.add_pending_deletion(
        chat_id, file_message_id, warning_message_id, delete_at
    )
    message_ids = [
        message_id
        for message_id in (file_message_id, warning_message_id)
        if message_id is not None
    ]
    run_at = datetime.fromtimestamp(max(delete_at, time.time() + 1), tz=timezone.utc)

    try:
        _schedule_job(row_id, chat_id, message_ids, run_at)
    except Exception:
        # Do not leave an orphaned row if the job could not be registered.
        await database.delete_pending_deletion(row_id)
        raise

    logger.info(
        "Scheduled deletion for chat %s (messages=%s) at %s",
        chat_id,
        message_ids,
        run_at.isoformat(),
    )
    return row_id


async def reload_pending_jobs() -> None:
    """Re-register deletion jobs after a restart.

    APScheduler already restores jobs from its own jobstore, but replaying the
    pending_deletions table repairs jobs lost if the scheduler DB was removed
    or a job was added while the scheduler was unavailable.
    """
    rows = await database.get_pending_deletions()
    now = time.time()

    for row in rows:
        message_ids = [
            message_id
            for message_id in (row["file_message_id"], row["warning_message_id"])
            if message_id is not None
        ]
        run_at = datetime.fromtimestamp(
            max(float(row["delete_at"]), now + 1), tz=timezone.utc
        )
        try:
            _schedule_job(int(row["id"]), int(row["chat_id"]), message_ids, run_at)
        except Exception:
            logger.exception("Failed to reload pending deletion row %s", row["id"])

    if rows:
        logger.info("Reloaded %s pending auto-delete job(s)", len(rows))


async def _delete_messages_job(
    row_id: int,
    chat_id: int,
    message_ids: list[int],
) -> None:
    """Delete a delivered file and its warning, then clear the pending row.

    This function is referenced by name from the persistent APScheduler
    jobstore. Renaming or moving it breaks already-scheduled jobs.
    """
    client = _client
    if client is None:
        logger.error(
            "Client is not available; leaving pending deletion %s for the next "
            "startup",
            row_id,
        )
        return

    for attempt in range(1, MAX_DELETE_RETRIES + 1):
        try:
            await client.delete_messages(chat_id=chat_id, message_ids=message_ids)
            break
        except FloodWait as exc:
            wait_seconds = int(exc.value) + 1
            logger.warning(
                "FloodWait while deleting chat %s messages: sleeping %ss "
                "(attempt %s/%s)",
                chat_id,
                wait_seconds,
                attempt,
                MAX_DELETE_RETRIES,
            )
            await asyncio.sleep(wait_seconds)
        except Exception:
            logger.exception(
                "Failed to delete chat %s messages %s", chat_id, message_ids
            )
            break

    try:
        await database.delete_pending_deletion(row_id)
    except Exception:
        logger.exception("Failed to clear pending deletion row %s", row_id)
