"""Token names, symbols and logos.

On-chain sources, in order of preference:

* the Metaplex Token Metadata account (a PDA derived from the mint),
* the Token-2022 ``tokenMetadata`` mint extension,
* the small built-in list in :mod:`solana_explorer.constants`.

The optional off-chain JSON referenced by the metadata ``uri`` (logo, description)
is fetched with strict limits and never from private or loopback addresses.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import socket
import struct
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from .constants import KNOWN_TOKENS
from .validation import b58decode, b58encode

METADATA_PROGRAM_ID = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"

# ed25519 curve parameters, used to tell whether a 32-byte value is a valid
# public key (on the curve) or a program-derived address (off the curve).
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)

OFFCHAIN_TIMEOUT = 4.0
OFFCHAIN_MAX_BYTES = 512 * 1024
OFFCHAIN_MAX_REDIRECTS = 3
IPFS_GATEWAY = "https://ipfs.io/ipfs/"


@dataclass(frozen=True)
class TokenMetadata:
    mint: str
    name: str | None
    symbol: str | None
    uri: str | None
    source: str  # "metaplex", "token-2022" or "builtin"


@dataclass(frozen=True)
class OffchainMetadata:
    image: str | None
    description: str | None
    website: str | None


# -- program-derived addresses -------------------------------------------------


def is_on_curve(point: bytes) -> bool:
    """Return ``True`` if *point* decompresses to a valid ed25519 curve point."""
    y = int.from_bytes(point, "little") & ((1 << 255) - 1)
    y2 = y * y % _P
    u = (y2 - 1) % _P
    v = (_D * y2 + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    if x2 == 0:
        return True
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    return (x * x - x2) % _P == 0


def find_program_address(seeds: list[bytes], program_id: str) -> tuple[str, int]:
    """Solana's ``findProgramAddress``: the first off-curve PDA, searching bumps 255..0."""
    program = b58decode(program_id)
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(
            b"".join(seeds) + bytes([bump]) + program + b"ProgramDerivedAddress"
        ).digest()
        if not is_on_curve(digest):
            return b58encode(digest), bump
    raise ValueError("unable to find a viable program address")


def metadata_address(mint: str) -> str:
    """Address of the Metaplex metadata account for *mint*."""
    program = b58decode(METADATA_PROGRAM_ID)
    return find_program_address([b"metadata", program, b58decode(mint)], METADATA_PROGRAM_ID)[0]


# -- parsing ------------------------------------------------------------------


def _read_string(data: bytes, offset: int) -> tuple[str, int]:
    (length,) = struct.unpack_from("<I", data, offset)
    offset += 4
    raw = data[offset : offset + length]
    if len(raw) != length:
        raise ValueError("truncated string")
    return raw.decode("utf-8", errors="replace").rstrip("\x00").strip(), offset + length


def parse_metaplex_metadata(data: bytes) -> tuple[str, str, str]:
    """Return ``(name, symbol, uri)`` from a Metaplex metadata account's data."""
    offset = 1 + 32 + 32  # key, update authority, mint
    name, offset = _read_string(data, offset)
    symbol, offset = _read_string(data, offset)
    uri, _ = _read_string(data, offset)
    return name, symbol, uri


def token2022_metadata(mint: str, parsed_mint: dict[str, Any] | None) -> TokenMetadata | None:
    """Read the ``tokenMetadata`` extension from a ``jsonParsed`` Token-2022 mint."""
    info = (parsed_mint or {}).get("info") or {}
    for extension in info.get("extensions") or []:
        if extension.get("extension") == "tokenMetadata":
            state = extension.get("state") or {}
            return TokenMetadata(
                mint,
                state.get("name") or None,
                state.get("symbol") or None,
                state.get("uri") or None,
                "token-2022",
            )
    return None


def metadata_from_account(mint: str, account: dict[str, Any] | None) -> TokenMetadata | None:
    """Decode a base64 ``getMultipleAccounts`` entry for a Metaplex metadata PDA."""
    if not account or account.get("owner") != METADATA_PROGRAM_ID:
        return None
    data = account.get("data")
    if not isinstance(data, list) or not data:
        return None
    try:
        name, symbol, uri = parse_metaplex_metadata(base64.b64decode(data[0]))
    except (ValueError, struct.error):
        return None
    return TokenMetadata(mint, name or None, symbol or None, uri or None, "metaplex")


def builtin_metadata(mint: str) -> TokenMetadata | None:
    symbol = KNOWN_TOKENS.get(mint)
    return TokenMetadata(mint, symbol, symbol, None, "builtin") if symbol else None


# -- off-chain JSON -----------------------------------------------------------


def _normalize_uri(uri: str) -> str | None:
    uri = uri.strip()
    if uri.startswith("ipfs://"):
        return IPFS_GATEWAY + uri.removeprefix("ipfs://").removeprefix("ipfs/")
    if uri.startswith("ar://"):
        return "https://arweave.net/" + uri.removeprefix("ar://")
    return uri if uri.startswith(("http://", "https://")) else None


def _is_public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            return False
    return bool(infos)


def fetch_offchain_metadata(
    uri: str | None, session: requests.Session | None = None
) -> OffchainMetadata | None:
    """Fetch the JSON a metadata ``uri`` points to; ``None`` on any problem."""
    url = _normalize_uri(uri or "")
    http = session or requests
    try:
        # Follow redirects by hand so every hop is checked against private networks.
        for _ in range(OFFCHAIN_MAX_REDIRECTS + 1):
            host = urlparse(url).hostname if url else None
            if not url or not host or not _is_public_host(host):
                return None
            with http.get(
                url, timeout=OFFCHAIN_TIMEOUT, stream=True, allow_redirects=False
            ) as resp:
                if resp.is_redirect:
                    url = _normalize_uri(urljoin(url, resp.headers.get("Location", "")))
                    continue
                if resp.status_code != 200:
                    return None
                body = b""
                for chunk in resp.iter_content(16384):
                    body += chunk
                    if len(body) > OFFCHAIN_MAX_BYTES:
                        return None
                break
        else:
            return None
        document = json.loads(body)
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    image = document.get("image")
    image = _normalize_uri(image) if isinstance(image, str) else None
    description = document.get("description")
    website = document.get("external_url") or (document.get("extensions") or {}).get("website")
    return OffchainMetadata(
        image=image,
        description=description[:1000] if isinstance(description, str) else None,
        website=_normalize_uri(website) if isinstance(website, str) else None,
    )
