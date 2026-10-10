import os

import pytest

from solana_explorer.errors import InvalidInputError
from solana_explorer.validation import (
    b58decode,
    b58encode,
    is_valid_address,
    is_valid_signature,
    split_identifiers,
    validate_address,
    validate_signature,
)

SYSTEM_PROGRAM = "11111111111111111111111111111111"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


@pytest.mark.parametrize("data", [b"\x00", b"\x00\x00\x01", b"hello", os.urandom(64)])
def test_base58_roundtrip(data):
    assert b58decode(b58encode(data)) == data


def test_base58_known_vectors():
    assert b58decode(SYSTEM_PROGRAM) == b"\x00" * 32
    assert b58encode(b"\x00" * 32) == SYSTEM_PROGRAM


@pytest.mark.parametrize("bad", ["", "0OIl", "abc!"])
def test_b58decode_rejects_invalid(bad):
    with pytest.raises(ValueError):
        b58decode(bad)


def test_addresses():
    assert is_valid_address(SYSTEM_PROGRAM)
    assert is_valid_address(f"  {USDC_MINT}\n")
    assert not is_valid_address(USDC_MINT[:-2])
    assert not is_valid_address("not-an-address")
    assert not is_valid_address(b58encode(os.urandom(64)))


def test_signatures(signature):
    assert is_valid_signature(signature)
    assert not is_valid_signature(USDC_MINT)
    assert not is_valid_signature("1" * 200)
    assert not is_valid_signature("INSERT_TRANSACTION_ID_HERE")


def test_validate_helpers_strip_and_raise(signature):
    assert validate_signature(f" {signature} ") == signature
    assert validate_address(f"\t{USDC_MINT}") == USDC_MINT
    with pytest.raises(InvalidInputError):
        validate_signature(USDC_MINT)
    with pytest.raises(InvalidInputError):
        validate_address("xyz")


def test_split_identifiers_dedupes_and_keeps_order():
    text = "b, a;\n c  a\tb,,"
    assert split_identifiers(text) == ["b", "a", "c"]
    assert split_identifiers(["x y", "y,z"]) == ["x", "y", "z"]
    assert split_identifiers("  \n ") == []
