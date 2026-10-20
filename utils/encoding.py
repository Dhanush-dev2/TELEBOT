"""Short, reversible IDs for deep links.

Telegram deep-link payloads allow A-Z, a-z, 0-9, "_" and "-" and are limited to
64 characters. Base62 of the storage message ID is short and URL-safe.
"""

from __future__ import annotations

ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
_BASE = len(ALPHABET)
_INDEX = {char: index for index, char in enumerate(ALPHABET)}


def encode_base62(number: int) -> str:
    """Encode a non-negative integer as a base62 string."""
    if number < 0:
        raise ValueError("number must be non-negative")
    if number == 0:
        return ALPHABET[0]

    chars: list[str] = []
    while number:
        number, remainder = divmod(number, _BASE)
        chars.append(ALPHABET[remainder])
    return "".join(reversed(chars))


def decode_base62(value: str) -> int:
    """Decode a base62 string. Raises ValueError for malformed input."""
    if not value:
        raise ValueError("value must not be empty")

    number = 0
    for char in value:
        try:
            digit = _INDEX[char]
        except KeyError as exc:
            raise ValueError(f"invalid base62 character: {char!r}") from exc
        number = number * _BASE + digit
    return number
