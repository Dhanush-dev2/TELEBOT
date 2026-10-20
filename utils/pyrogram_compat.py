"""Runtime compatibility fixes for Pyrogram's outdated peer-ID bounds.

Pyrogram 2.0.106 still uses bounds from before Telegram expanded its ID
ranges. Valid newer channels (for example -1004479275660) otherwise raise
"ValueError: Peer id invalid" inside ``get_peer_type()`` before any API
request is made. Only widen the bounds, so a future Pyrogram version with
newer values wins.
"""

from __future__ import annotations

from pyrogram import utils as pyrogram_utils

# Ranges: https://core.telegram.org/api/bots/ids
if pyrogram_utils.MIN_CHANNEL_ID > -1_997_852_516_352:
    pyrogram_utils.MIN_CHANNEL_ID = -1_997_852_516_352
if pyrogram_utils.MIN_CHAT_ID > -999_999_999_999:
    pyrogram_utils.MIN_CHAT_ID = -999_999_999_999
