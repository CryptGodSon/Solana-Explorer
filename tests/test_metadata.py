import base64
import struct

import pytest

from solana_explorer import metadata
from solana_explorer.metadata import (
    METADATA_PROGRAM_ID,
    fetch_offchain_metadata,
    find_program_address,
    is_on_curve,
    metadata_address,
    metadata_from_account,
    parse_metaplex_metadata,
    token2022_metadata,
)
from solana_explorer.validation import b58decode

BONK = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
# A real wallet (it signed transactions), so it must be a point on the curve.
WALLET = "7JCe3GHwkEr3feHgtLXnmuJ1yB3A7coSeyynxTBgdG8k"


def borsh_string(value: str, padded: int) -> bytes:
    raw = value.encode().ljust(padded, b"\x00")
    return struct.pack("<I", len(raw)) + raw


def metaplex_account(name: str, symbol: str, uri: str) -> bytes:
    return (
        b"\x04"
        + b"\x01" * 32
        + b"\x02" * 32
        + borsh_string(name, 32)
        + borsh_string(symbol, 10)
        + borsh_string(uri, 200)
        + b"\x00" * 50
    )


def test_metadata_pda_matches_mainnet():
    # Verified against mainnet: these accounts exist and hold the Bonk / USDC metadata.
    assert metadata_address(BONK) == "FDZZbyY9XGpL3CNKUZxLk3wFTTQYL3TkDiDzqxrizcPN"
    assert metadata_address(USDC) == "5x38Kp4hvdomTCnCrAny4UtMUt5rQBdB6px2K1Ui45Wq"


def test_curve_check():
    assert is_on_curve(b58decode(WALLET))
    assert not is_on_curve(b58decode(metadata_address(BONK)))
    address, bump = find_program_address([b"seed"], METADATA_PROGRAM_ID)
    assert 0 <= bump <= 255
    assert not is_on_curve(b58decode(address))


def test_parse_metaplex_metadata_strips_padding():
    data = metaplex_account("Bonk", "Bonk", "https://arweave.net/abc")
    assert parse_metaplex_metadata(data) == ("Bonk", "Bonk", "https://arweave.net/abc")


def test_metadata_from_account_checks_owner_and_encoding():
    encoded = base64.b64encode(metaplex_account("Jupiter", "JUP", "")).decode()
    account = {"owner": METADATA_PROGRAM_ID, "data": [encoded, "base64"]}
    md = metadata_from_account("Mint", account)
    assert (md.name, md.symbol, md.uri, md.source) == ("Jupiter", "JUP", None, "metaplex")
    assert metadata_from_account("Mint", {**account, "owner": "Other"}) is None
    assert metadata_from_account("Mint", None) is None
    truncated = {"owner": METADATA_PROGRAM_ID, "data": [base64.b64encode(b"\x04").decode()]}
    assert metadata_from_account("Mint", truncated) is None


def test_token2022_metadata_extension():
    parsed = {
        "info": {
            "extensions": [
                {"extension": "metadataPointer", "state": {}},
                {"extension": "tokenMetadata", "state": {"name": "Paypal USD", "symbol": "PYUSD"}},
            ]
        }
    }
    md = token2022_metadata("Mint", parsed)
    assert (md.name, md.symbol, md.source) == ("Paypal USD", "PYUSD", "token-2022")
    assert token2022_metadata("Mint", {"info": {}}) is None


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("ipfs://Qm123", "https://ipfs.io/ipfs/Qm123"),
        ("ar://abc", "https://arweave.net/abc"),
        ("https://x.io/a.json", "https://x.io/a.json"),
        ("file:///etc/passwd", None),
        ("javascript:alert(1)", None),
    ],
)
def test_normalize_uri(uri, expected):
    assert metadata._normalize_uri(uri) == expected


def test_offchain_fetch_refuses_private_addresses():
    assert fetch_offchain_metadata("http://127.0.0.1:8000/meta.json") is None
    assert fetch_offchain_metadata("http://localhost/meta.json") is None
    assert fetch_offchain_metadata(None) is None


class FakeStream:
    def __init__(self, status, body=b"", location=None):
        self.status_code = status
        self.body = body
        self.headers = {"Location": location} if location else {}
        self.is_redirect = location is not None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, size):
        yield self.body


class FakeHttp:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return self.responses.pop(0)


def test_offchain_fetch_follows_checked_redirects(monkeypatch):
    monkeypatch.setattr(metadata, "_is_public_host", lambda host: host != "internal.lan")
    body = b'{"image": "ipfs://QmLogo", "description": "A token", "external_url": "https://t.io"}'
    http = FakeHttp(FakeStream(302, location="https://cdn.example/m.json"), FakeStream(200, body))
    result = fetch_offchain_metadata("https://example.com/m.json", session=http)
    assert result.image == "https://ipfs.io/ipfs/QmLogo"
    assert result.description == "A token"
    assert result.website == "https://t.io"
    assert http.urls == ["https://example.com/m.json", "https://cdn.example/m.json"]

    # A redirect into the local network is not followed.
    http = FakeHttp(FakeStream(302, location="http://internal.lan/secret"))
    assert fetch_offchain_metadata("https://example.com/m.json", session=http) is None


def test_offchain_fetch_rejects_oversized_or_invalid(monkeypatch):
    monkeypatch.setattr(metadata, "_is_public_host", lambda host: True)
    huge = FakeHttp(FakeStream(200, b"x" * (metadata.OFFCHAIN_MAX_BYTES + 1)))
    assert fetch_offchain_metadata("https://example.com/m.json", session=huge) is None
    not_json = FakeHttp(FakeStream(200, b"<html>"))
    assert fetch_offchain_metadata("https://example.com/m.json", session=not_json) is None
