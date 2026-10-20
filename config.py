"""Load and validate configuration from environment variables / .env.

Importing this module validates the configuration. If anything required is
missing or malformed the process exits immediately with an actionable error
instead of failing later in an obscure place.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from dotenv import load_dotenv

from utils.categories import parse_chat_ref, parse_library_channels

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

# Load .env from the repository root even when the bot is started from another
# working directory (systemd, Docker, cron, ...).
load_dotenv(dotenv_path=ENV_FILE)

ChatRef = Union[int, str]

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
_SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _get(name: str, default: Optional[str] = None) -> Optional[str]:
    """Read a trimmed environment variable, treating blank values as unset."""
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw or default


def _resolve_path(value: str) -> Path:
    """Resolve relative paths against the repository root, not the CWD."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path.resolve()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    api_id: int
    api_hash: str
    storage_channel: ChatRef
    links_channel: Optional[ChatRef]
    library_channels: dict[str, ChatRef]
    admin_ids: tuple[int, ...]
    vplink_api_key: str
    bot_username: str
    force_sub_channel: Optional[ChatRef]
    force_sub_invite_link: Optional[str]
    auto_delete_seconds: int
    turso_database_url: str
    turso_auth_token: Optional[str]
    port: int
    data_dir: Path
    session_name: str
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        errors: list[str] = []

        def required(name: str) -> str:
            value = _get(name)
            if not value:
                errors.append(f"{name} is required")
                return ""
            return value

        bot_token = required("BOT_TOKEN")
        api_hash = required("API_HASH")
        vplink_api_key = required("VPLINK_API_KEY")
        bot_username = required("BOT_USERNAME").lstrip("@")
        storage_raw = required("STORAGE_CHANNEL")
        turso_database_url = required("TURSO_DATABASE_URL")
        turso_auth_token = _get("TURSO_AUTH_TOKEN")

        if turso_database_url and not turso_database_url.startswith(
            ("libsql://", "wss://", "ws://", "https://", "file:")
        ):
            errors.append(
                "TURSO_DATABASE_URL must be a libsql:// or https:// URL from "
                "'turso db show <db> --url'. Use file:path only for local testing."
            )
        if (
            turso_database_url
            and not turso_database_url.startswith("file:")
            and not turso_auth_token
        ):
            errors.append(
                "TURSO_AUTH_TOKEN is required for a remote Turso database"
            )

        api_id = 0
        raw_api_id = required("API_ID")
        if raw_api_id:
            try:
                api_id = int(raw_api_id)
            except ValueError:
                errors.append("API_ID must be an integer")
            else:
                if api_id <= 0:
                    errors.append("API_ID must be a positive integer")

        admin_ids: tuple[int, ...] = ()
        raw_admins = required("ADMIN_IDS")
        if raw_admins:
            parsed_admins: list[int] = []
            for part in raw_admins.split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    parsed_admins.append(int(part))
                except ValueError:
                    errors.append(f"ADMIN_IDS contains a non-numeric value: {part!r}")
            if parsed_admins:
                admin_ids = tuple(sorted(set(parsed_admins)))
            else:
                errors.append(
                    "ADMIN_IDS must contain at least one numeric Telegram user ID"
                )

        auto_delete_seconds = 600
        raw_auto_delete = _get("AUTO_DELETE_SECONDS", "600")
        if raw_auto_delete:
            try:
                auto_delete_seconds = int(raw_auto_delete)
            except ValueError:
                errors.append("AUTO_DELETE_SECONDS must be an integer number of seconds")
            else:
                if auto_delete_seconds <= 0:
                    errors.append("AUTO_DELETE_SECONDS must be greater than zero")

        port = 8080
        raw_port = _get("PORT", "8080")
        if raw_port:
            try:
                port = int(raw_port)
            except ValueError:
                errors.append("PORT must be an integer")
            else:
                if not 1 <= port <= 65535:
                    errors.append("PORT must be between 1 and 65535")

        force_sub_channel: Optional[ChatRef] = None
        raw_force_sub = _get("FORCE_SUB_CHANNEL")
        if raw_force_sub:
            force_sub_channel = parse_chat_ref(raw_force_sub)

        links_channel: Optional[ChatRef] = None
        raw_links = _get("LINKS_CHANNEL")
        if raw_links:
            links_channel = parse_chat_ref(raw_links)

        library_channels, library_channel_errors = parse_library_channels(
            _get("LIBRARY_CHANNELS"), _get("LIBRARY_CHANNEL")
        )
        errors.extend(library_channel_errors)

        force_sub_invite_link = _get("FORCE_SUB_INVITE_LINK")
        if force_sub_invite_link and not force_sub_invite_link.startswith(
            ("https://t.me/", "http://t.me/")
        ):
            errors.append(
                "FORCE_SUB_INVITE_LINK must be a https://t.me/... invite link"
            )

        if bot_username and not _USERNAME_RE.fullmatch(bot_username):
            errors.append(
                "BOT_USERNAME must be the bot's username without @ "
                "(5-32 letters, digits or underscores)"
            )

        log_level = (_get("LOG_LEVEL", "INFO") or "INFO").upper()
        if log_level not in _LOG_LEVELS:
            errors.append(
                f"LOG_LEVEL must be one of: {', '.join(sorted(_LOG_LEVELS))}"
            )

        data_dir = _resolve_path(_get("DATA_DIR", "data") or "data")
        session_name = _get("SESSION_NAME", "telebot") or "telebot"
        if not _SESSION_NAME_RE.fullmatch(session_name):
            errors.append(
                "SESSION_NAME may only contain letters, digits, dots, dashes "
                "and underscores"
            )

        storage_channel: ChatRef = (
            parse_chat_ref(storage_raw) if storage_raw else ""
        )

        if errors:
            print("Configuration error(s):", file=sys.stderr)
            for error in errors:
                print(f"  - {error}", file=sys.stderr)
            print(
                f"\nCreate {ENV_FILE} from {BASE_DIR / '.env.example'} "
                "and fill in the values.",
                file=sys.stderr,
            )
            raise SystemExit(1)

        data_dir.mkdir(parents=True, exist_ok=True)

        return cls(
            bot_token=bot_token,
            api_id=api_id,
            api_hash=api_hash,
            storage_channel=storage_channel,
            links_channel=links_channel,
            library_channels=library_channels,
            admin_ids=admin_ids,
            vplink_api_key=vplink_api_key,
            bot_username=bot_username,
            force_sub_channel=force_sub_channel,
            force_sub_invite_link=force_sub_invite_link,
            auto_delete_seconds=auto_delete_seconds,
            turso_database_url=turso_database_url,
            turso_auth_token=turso_auth_token,
            port=port,
            data_dir=data_dir,
            session_name=session_name,
            log_level=log_level,
        )


settings = Settings.from_env()
