"""VPLink URL-shortener client.

API contract used here (as documented by the bot owner):
    GET https://vplink.in/api?api=API_KEY&url=LONG_URL&format=json
    -> JSON containing a "shortenedUrl" field.

Shortening is best-effort: a failure is logged and the caller falls back to the
direct Telegram link, so a third-party outage never blocks file delivery.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

from config import settings

logger = logging.getLogger(__name__)

VPLINK_ENDPOINT = "https://vplink.in/api"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
MAX_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 2


async def shorten_url(long_url: str) -> Optional[str]:
    """Return a shortened URL, or None if the service cannot be reached."""
    params = {
        "api": settings.vplink_api_key,
        "url": long_url,
        "format": "json",
    }
    headers = {"User-Agent": "telegram-file-bot/1.0"}

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                async with session.get(
                    VPLINK_ENDPOINT, params=params, headers=headers
                ) as response:
                    if response.status != 200:
                        body = await response.text()
                        logger.warning(
                            "VPLink returned HTTP %s: %s",
                            response.status,
                            body[:200],
                        )
                    else:
                        data = await response.json(content_type=None)
                        if not isinstance(data, dict):
                            logger.warning("VPLink returned unexpected JSON: %r", data)
                        else:
                            short_url = data.get("shortenedUrl") or data.get(
                                "shortened_url"
                            )
                            if isinstance(short_url, str):
                                short_url = short_url.strip()
                            if isinstance(short_url, str) and short_url.startswith(
                                ("http://", "https://")
                            ):
                                return short_url
                            logger.warning(
                                "VPLink response has no shortenedUrl (keys=%s)",
                                sorted(data.keys()),
                            )
            except asyncio.TimeoutError:
                logger.warning(
                    "VPLink request timed out (attempt %s/%s)",
                    attempt,
                    MAX_ATTEMPTS,
                )
            except aiohttp.ClientError as exc:
                logger.warning(
                    "VPLink request failed (attempt %s/%s): %s",
                    attempt,
                    MAX_ATTEMPTS,
                    exc,
                )
            except ValueError:
                logger.warning("VPLink returned invalid JSON")

            if attempt < MAX_ATTEMPTS:
                await asyncio.sleep(RETRY_DELAY_SECONDS)

    return None
