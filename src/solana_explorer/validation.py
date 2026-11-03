"""Validation of base58-encoded Solana identifiers."""

from __future__ import annotations

import re
from collections.abc import Iterable

from .errors import InvalidInputError

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_INDEX = {char: index for index, char in enumerate(BASE58_ALPHABET)}

SIGNATURE_BYTES = 64
PUBKEY_BYTES = 32
# A 64-byte value never needs more than 88 base58 characters; anything longer
# is rejected before decoding so huge inputs cannot cause slow big-int math.
_MAX_ENCODED_LENGTH = 88

_SEPARATORS = re.compile(r"[\s,;]+")


def b58decode(value: str) -> bytes:
    """Decode a base58 (Bitcoin alphabet) string into bytes."""
    if not value:
        raise ValueError("empty string is not valid base58")
    number = 0
    for char in value:
        try:
            number = number * 58 + _BASE58_INDEX[char]
        except KeyError:
            raise ValueError(f"invalid base58 character {char!r}") from None
    body = number.to_bytes((number.bit_length() + 7) // 8, "big")
    leading_zeros = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading_zeros + body


def b58encode(data: bytes) -> str:
    """Encode bytes as a base58 (Bitcoin alphabet) string."""
    number = int.from_bytes(data, "big")
    chars: list[str] = []
    while number:
        number, remainder = divmod(number, 58)
        chars.append(BASE58_ALPHABET[remainder])
    leading_zeros = len(data) - len(data.lstrip(b"\x00"))
    return "1" * leading_zeros + "".join(reversed(chars))


def _decoded_length(value: str) -> int | None:
    if not value or len(value) > _MAX_ENCODED_LENGTH:
        return None
    try:
        return len(b58decode(value))
    except ValueError:
        return None


def is_valid_signature(value: str) -> bool:
    """Return ``True`` if *value* is a base58-encoded 64-byte transaction signature."""
    return _decoded_length(value.strip()) == SIGNATURE_BYTES


def is_valid_address(value: str) -> bool:
    """Return ``True`` if *value* is a base58-encoded 32-byte public key."""
    return _decoded_length(value.strip()) == PUBKEY_BYTES


def validate_signature(value: str) -> str:
    """Return the stripped signature or raise :class:`InvalidInputError`."""
    candidate = value.strip()
    if not is_valid_signature(candidate):
        raise InvalidInputError(f"{candidate!r} is not a valid transaction signature")
    return candidate


def validate_address(value: str) -> str:
    """Return the stripped address or raise :class:`InvalidInputError`."""
    candidate = value.strip()
    if not is_valid_address(candidate):
        raise InvalidInputError(f"{candidate!r} is not a valid Solana address")
    return candidate


def split_identifiers(text: str | Iterable[str]) -> list[str]:
    """Split free-form input (commas, semicolons, whitespace) into unique tokens.

    Order is preserved and duplicates are dropped, so the result can be fed
    straight into a batch lookup.
    """
    chunks = [text] if isinstance(text, str) else list(text)
    seen: dict[str, None] = {}
    for chunk in chunks:
        for token in _SEPARATORS.split(chunk):
            if token:
                seen.setdefault(token, None)
    return list(seen)
