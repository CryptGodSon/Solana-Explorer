from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from solana_explorer.validation import b58encode

FIXTURES = Path(__file__).parent / "fixtures"


def make_address(seed: int) -> str:
    return b58encode(bytes([seed + 1]) * 32)


def make_signature(seed: int) -> str:
    return b58encode(bytes([seed + 1]) * 64)


@pytest.fixture(scope="session")
def _raw_transaction() -> dict[str, Any]:
    return json.loads((FIXTURES / "transfer_transaction.json").read_text(encoding="utf-8"))


@pytest.fixture
def raw_transaction(_raw_transaction: dict[str, Any]) -> dict[str, Any]:
    """A fresh copy of a jsonParsed ``getTransaction`` result (SOL + USDC transfers)."""
    return copy.deepcopy(_raw_transaction)


@pytest.fixture
def signature(raw_transaction: dict[str, Any]) -> str:
    return raw_transaction["transaction"]["signatures"][0]


class FakeClient:
    """Stands in for ``SolanaRpcClient`` and records every call."""

    endpoint = "https://api.mainnet-beta.solana.com"

    def __init__(
        self,
        transactions: dict[str, dict[str, Any]] | None = None,
        accounts: dict[str, dict[str, Any]] | None = None,
        signatures: list[dict[str, Any]] | None = None,
    ) -> None:
        self.transactions = transactions or {}
        self.accounts = accounts or {}
        self.signatures = signatures or []
        self.calls: list[tuple[str, Any]] = []

    def get_transaction(self, signature: str) -> dict[str, Any] | None:
        self.calls.append(("getTransaction", signature))
        return self.transactions.get(signature)

    def get_account_info(self, address: str) -> dict[str, Any] | None:
        self.calls.append(("getAccountInfo", address))
        return self.accounts.get(address)

    def get_signatures_for_address(
        self, address: str, *, limit: int = 10, before: str | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append(("getSignaturesForAddress", (address, limit, before)))
        return self.signatures[:limit]


@pytest.fixture
def fake_client(raw_transaction: dict[str, Any], signature: str) -> FakeClient:
    return FakeClient(transactions={signature: raw_transaction})
