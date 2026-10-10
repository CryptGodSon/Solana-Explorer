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
        # Tests set these to whatever the scenario needs.
        self.tip = 1000
        self.produced: list[int] = []
        self.blocks: dict[int, Any] = {}
        self.epoch: dict[str, Any] = {}
        self.samples: list[dict[str, Any]] = []
        self.multiple: dict[str, dict[str, Any]] = {}  # "<encoding>:<address>" -> account
        self.token_accounts: dict[str, list[dict[str, Any]]] = {}  # program id -> entries
        self.largest: list[dict[str, Any]] = []

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

    # Network / block / token methods used by the web UI.

    def get_slot(self) -> int:
        self.calls.append(("getSlot", None))
        return self.tip

    def get_blocks(self, start_slot: int, end_slot: int) -> list[int]:
        self.calls.append(("getBlocks", (start_slot, end_slot)))
        return [s for s in self.produced if start_slot <= s <= end_slot]

    def get_block(self, slot: int, *, transaction_details: str = "signatures", rewards=True):
        self.calls.append(("getBlock", (slot, transaction_details)))
        block = self.blocks.get(slot)
        if isinstance(block, Exception):
            raise block
        return block

    def get_epoch_info(self) -> dict[str, Any]:
        return self.epoch

    def get_recent_performance_samples(self, limit: int = 30) -> list[dict[str, Any]]:
        return self.samples[:limit]

    def get_supply(self) -> dict[str, Any]:
        return {"total": 600 * 10**15, "circulating": 500 * 10**15}

    def get_version(self) -> dict[str, Any]:
        return {"solana-core": "9.9.9"}

    def get_multiple_accounts(self, addresses: list[str], *, encoding: str = "base64"):
        self.calls.append(("getMultipleAccounts", (tuple(addresses), encoding)))
        return [self.multiple.get(f"{encoding}:{a}") for a in addresses]

    def get_token_accounts_by_owner(self, owner: str, program_id: str):
        return self.token_accounts.get(program_id, [])

    def get_token_largest_accounts(self, mint: str):
        return self.largest


@pytest.fixture
def fake_client(raw_transaction: dict[str, Any], signature: str) -> FakeClient:
    return FakeClient(transactions={signature: raw_transaction})
