"""High-level API: validated, cached lookups that return typed models."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol

from .cache import LRUCache, TTLCache
from .errors import (
    InvalidInputError,
    NotFoundError,
    RpcError,
    SolanaExplorerError,
    TransactionNotFoundError,
)
from .metadata import (
    OffchainMetadata,
    TokenMetadata,
    builtin_metadata,
    fetch_offchain_metadata,
    metadata_address,
    metadata_from_account,
    token2022_metadata,
)
from .models import (
    AccountSummary,
    BlockSummary,
    BlockTransaction,
    NetworkStats,
    PerformanceSample,
    SignatureInfo,
    TokenHolder,
    TokenHolding,
    TokenInfo,
    TransactionDetails,
    scale_amount,
)
from .rpc import SolanaRpcClient
from .validation import split_identifiers, validate_address, validate_signature

MAX_HISTORY_LIMIT = 1000
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
TOKEN_PROGRAM_IDS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
)
TOKEN_PROGRAMS = ("spl-token", "spl-token-2022")
# getBlock error codes meaning "there is no block for this slot on this node"
BLOCK_UNAVAILABLE_CODES = frozenset({-32004, -32007, -32009, -32014})
MULTIPLE_ACCOUNTS_LIMIT = 100
_MISSING = object()


class RpcClient(Protocol):
    endpoint: str

    def get_transaction(self, signature: str) -> dict[str, Any] | None: ...

    def get_account_info(self, address: str) -> dict[str, Any] | None: ...

    def get_signatures_for_address(
        self, address: str, *, limit: int = 10, before: str | None = None
    ) -> list[dict[str, Any]]: ...

    # Used by the network, block and token helpers (and the web UI).
    def get_block(
        self, slot: int, *, transaction_details: str = "signatures", rewards: bool = True
    ) -> dict[str, Any] | None: ...

    def get_blocks(self, start_slot: int, end_slot: int) -> list[int]: ...

    def get_slot(self) -> int: ...

    def get_epoch_info(self) -> dict[str, Any]: ...

    def get_recent_performance_samples(self, limit: int = 30) -> list[dict[str, Any]]: ...

    def get_supply(self) -> dict[str, Any]: ...

    def get_version(self) -> dict[str, Any]: ...

    def get_multiple_accounts(
        self, addresses: list[str], *, encoding: str = "base64"
    ) -> list[dict[str, Any] | None]: ...

    def get_token_accounts_by_owner(self, owner: str, program_id: str) -> list[dict[str, Any]]: ...

    def get_token_largest_accounts(self, mint: str) -> list[dict[str, Any]]: ...


def account_kind(account: AccountSummary) -> str:
    """Classify an account the way block explorers label it."""
    if not account.exists:
        return "empty"
    if account.executable:
        return "program"
    if account.program in TOKEN_PROGRAMS:
        if account.account_type == "mint":
            return "token_mint"
        if account.account_type == "account":
            return "token_account"
    if account.program in ("vote", "stake", "nonce"):
        return account.program
    if account.owner == SYSTEM_PROGRAM_ID:
        return "wallet"
    return "account"


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


@dataclass(frozen=True)
class TransactionResult:
    """Outcome of one lookup in a batch: either ``transaction`` or ``error`` is set."""

    signature: str
    transaction: TransactionDetails | None = None
    error: SolanaExplorerError | None = None

    @property
    def ok(self) -> bool:
        return self.transaction is not None


class SolanaExplorer:
    """Explore transactions and accounts on a Solana cluster.

    Example::

        explorer = SolanaExplorer(endpoint="devnet")
        tx = explorer.get_transaction(signature)
        print(tx.fee_sol, tx.success)
    """

    def __init__(
        self,
        client: RpcClient | None = None,
        *,
        endpoint: str | None = None,
        cache_size: int = 256,
        **client_options: Any,
    ) -> None:
        if client is not None and (endpoint is not None or client_options):
            raise TypeError("pass either a client or endpoint/client options, not both")
        self.client: RpcClient = client or SolanaRpcClient(endpoint, **client_options)
        self._transactions: LRUCache[str, TransactionDetails] = LRUCache(cache_size)
        # Blocks and token metadata practically never change; network numbers do.
        self._blocks: LRUCache[tuple[int, str], BlockSummary] = LRUCache(128)
        self._metadata: LRUCache[str, Any] = LRUCache(4096)
        self._offchain: LRUCache[str, Any] = LRUCache(1024)
        self._short_lived: TTLCache[str, Any] = TTLCache(ttl=4.0)
        self._long_lived: TTLCache[str, Any] = TTLCache(ttl=600.0)

    @property
    def endpoint(self) -> str:
        return self.client.endpoint

    def get_transaction(self, signature: str) -> TransactionDetails:
        """Fetch and decode one transaction. Results are cached per explorer instance."""
        signature = validate_signature(signature)
        cached = self._transactions.get(signature)
        if cached is not None:
            return cached
        raw = self.client.get_transaction(signature)
        if raw is None:
            raise TransactionNotFoundError(signature)
        details = TransactionDetails.from_rpc(signature, raw)
        self._transactions.put(signature, details)
        return details

    def iter_transactions(self, signatures: str | Iterable[str]) -> Iterator[TransactionResult]:
        """Look up many transactions, yielding one result per unique signature.

        Errors are captured per signature so one bad input does not abort the batch.
        """
        for signature in split_identifiers(signatures):
            try:
                yield TransactionResult(signature, transaction=self.get_transaction(signature))
            except SolanaExplorerError as exc:
                yield TransactionResult(signature, error=exc)

    def get_transactions(self, signatures: str | Iterable[str]) -> list[TransactionResult]:
        return list(self.iter_transactions(signatures))

    def get_account(self, address: str) -> AccountSummary:
        address = validate_address(address)
        return AccountSummary.from_rpc(address, self.client.get_account_info(address))

    def get_signatures(
        self, address: str, *, limit: int = 10, before: str | None = None
    ) -> list[SignatureInfo]:
        """Most recent transaction signatures involving *address*, newest first."""
        address = validate_address(address)
        limit = max(1, min(limit, MAX_HISTORY_LIMIT))
        if before is not None:
            before = validate_signature(before)
        raw = self.client.get_signatures_for_address(address, limit=limit, before=before)
        return [SignatureInfo.from_rpc(item) for item in raw]

    def clear_cache(self) -> None:
        for cache in (
            self._transactions,
            self._blocks,
            self._metadata,
            self._offchain,
            self._short_lived,
            self._long_lived,
        ):
            cache.clear()

    # -- network ---------------------------------------------------------------

    def _optional(self, cache: TTLCache[str, Any], key: str, fetch: Any) -> Any:
        """Fetch a non-essential value, caching it and swallowing RPC failures."""
        value = cache.get(key)
        if value is None:
            try:
                value = fetch()
            except SolanaExplorerError:
                return None
            cache.put(key, value)
        return value

    def network_stats(self) -> NetworkStats:
        """Epoch, slot, TPS history, SOL supply and node version."""
        cached = self._short_lived.get("network")
        if cached is not None:
            return cached
        epoch = self.client.get_epoch_info()
        samples = self._optional(
            self._short_lived, "samples", lambda: self.client.get_recent_performance_samples(30)
        )
        # getSupply takes several seconds on public nodes, so only use a cached value
        # here; ``supply()`` fills the cache.
        supply = self._long_lived.get("supply") or {}
        version = self._optional(self._long_lived, "version", self.client.get_version) or {}
        stats = NetworkStats(
            epoch=int(epoch.get("epoch", 0)),
            slot_index=int(epoch.get("slotIndex", 0)),
            slots_in_epoch=int(epoch.get("slotsInEpoch", 0)),
            absolute_slot=int(epoch.get("absoluteSlot", 0)),
            block_height=epoch.get("blockHeight"),
            transaction_count=epoch.get("transactionCount"),
            samples=tuple(PerformanceSample.from_rpc(s) for s in samples or []),
            total_supply_lamports=supply.get("total"),
            circulating_supply_lamports=supply.get("circulating"),
            node_version=version.get("solana-core"),
        )
        self._short_lived.put("network", stats)
        return stats

    def supply(self) -> dict[str, Any]:
        """Total, circulating and non-circulating SOL supply in lamports (cached 10 min)."""
        cached = self._long_lived.get("supply")
        if cached is None:
            cached = self.client.get_supply()
            self._long_lived.put("supply", cached)
            self._short_lived.clear()  # let the next network_stats() include it
        return cached

    # -- blocks ------------------------------------------------------------------

    def _recent_block_slots(self, count: int) -> list[int]:
        """Slots of the newest *count* produced blocks, newest first."""
        cached = self._short_lived.get(f"recent-slots-{count}")
        if cached is not None:
            return cached
        tip = self.client.get_slot()
        # Some slots are skipped by their leader, so look a bit further back.
        slots = self.client.get_blocks(max(0, tip - count * 3 - 10), tip)
        newest = sorted(slots, reverse=True)[:count]
        self._short_lived.put(f"recent-slots-{count}", newest)
        return newest

    def _fetch_block(self, slot: int, details: str) -> BlockSummary:
        key = (slot, details)
        cached = self._blocks.get(key)
        if cached is not None:
            return cached
        try:
            raw = self.client.get_block(slot, transaction_details=details)
        except RpcError as exc:
            if exc.code in BLOCK_UNAVAILABLE_CODES:
                message = f"No block is available for slot {slot}: {exc.message}"
                raise NotFoundError(message) from exc
            raise
        if raw is None:
            raise NotFoundError(f"No block is available for slot {slot}")
        block = BlockSummary.from_rpc(slot, raw)
        self._blocks.put(key, block)
        return block

    def get_block(self, slot: int) -> BlockSummary:
        """A block with its transactions (signature, status, fee, signer, vote flag)."""
        if slot < 0:
            raise InvalidInputError("slot must be a non-negative integer")
        return self._fetch_block(slot, "accounts")

    def latest_blocks(self, limit: int = 8) -> list[BlockSummary]:
        slots = self._recent_block_slots(max(1, min(limit, 20)))
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(self._fetch_block, slot, "signatures") for slot in slots]
            blocks = []
            for future in futures:
                try:
                    blocks.append(future.result())
                except NotFoundError:
                    continue
        return blocks

    def latest_transactions(self, limit: int = 15) -> tuple[BlockSummary, list[BlockTransaction]]:
        """Non-vote transactions from the newest available block."""
        for slot in self._recent_block_slots(3):
            try:
                block = self._fetch_block(slot, "accounts")
            except NotFoundError:
                continue
            return block, [tx for tx in block.transactions if not tx.is_vote][:limit]
        raise NotFoundError("No recent block is available")

    # -- tokens ------------------------------------------------------------------

    def token_metadata(self, mints: Iterable[str]) -> dict[str, TokenMetadata]:
        """Name/symbol/uri for each mint that has any (Metaplex, Token-2022 or built-in)."""
        unique = list(dict.fromkeys(mints))
        result: dict[str, TokenMetadata] = {}
        missing = []
        for mint in unique:
            cached = self._metadata.get(mint)
            if cached is None:
                missing.append(mint)
            elif cached is not _MISSING:
                result[mint] = cached
        if not missing:
            return result

        found: dict[str, TokenMetadata] = {}
        for chunk in _chunks(missing, MULTIPLE_ACCOUNTS_LIMIT):
            accounts = self.client.get_multiple_accounts([metadata_address(m) for m in chunk])
            for mint, account in zip(chunk, accounts, strict=False):
                metadata = metadata_from_account(mint, account)
                if metadata is not None:
                    found[mint] = metadata

        remaining = [m for m in missing if m not in found]
        for chunk in _chunks(remaining, MULTIPLE_ACCOUNTS_LIMIT):
            accounts = self.client.get_multiple_accounts(chunk, encoding="jsonParsed")
            for mint, account in zip(chunk, accounts, strict=False):
                data = (account or {}).get("data")
                parsed = data.get("parsed") if isinstance(data, dict) else None
                metadata = token2022_metadata(mint, parsed) or builtin_metadata(mint)
                if metadata is not None:
                    found[mint] = metadata

        for mint in missing:
            self._metadata.put(mint, found.get(mint, _MISSING))
        result.update(found)
        return result

    def offchain_metadata(self, uri: str | None) -> OffchainMetadata | None:
        if not uri:
            return None
        cached = self._offchain.get(uri)
        if cached is None:
            cached = fetch_offchain_metadata(uri) or _MISSING
            self._offchain.put(uri, cached)
        return None if cached is _MISSING else cached

    def get_token_holdings(self, owner: str) -> list[TokenHolding]:
        """SPL Token and Token-2022 balances of *owner*, non-empty and named ones first."""
        owner = validate_address(owner)
        holdings: list[TokenHolding] = []
        for program_id in TOKEN_PROGRAM_IDS:
            for raw in self.client.get_token_accounts_by_owner(owner, program_id):
                holding = TokenHolding.from_rpc(raw)
                if holding is not None:
                    holdings.append(holding)
        metadata = self.token_metadata(h.mint for h in holdings) if holdings else {}
        named = [
            dataclasses.replace(
                h,
                name=metadata[h.mint].name if h.mint in metadata else None,
                symbol=metadata[h.mint].symbol if h.mint in metadata else None,
            )
            for h in holdings
        ]
        return sorted(named, key=lambda h: (h.amount == 0, h.symbol is None, -h.amount))

    def get_token_info(self, account: AccountSummary) -> TokenInfo:
        """Supply, authorities and metadata (including the off-chain logo) of a mint account."""
        info = TokenInfo.from_account(account)
        metadata = self.token_metadata([account.address]).get(account.address)
        if metadata is None:
            return info
        offchain = self.offchain_metadata(metadata.uri)
        return dataclasses.replace(
            info,
            name=metadata.name,
            symbol=metadata.symbol,
            metadata_source=metadata.source,
            uri=metadata.uri,
            image=offchain.image if offchain else None,
            description=offchain.description if offchain else None,
            website=offchain.website if offchain else None,
        )

    def get_top_holders(self, mint: str, supply: Any = None) -> list[TokenHolder]:
        """The largest token accounts of *mint* (rate-limited on public RPC nodes)."""
        mint = validate_address(mint)
        holders = []
        for raw in self.client.get_token_largest_accounts(mint):
            amount = scale_amount(int(raw.get("amount", 0)), int(raw.get("decimals", 0)))
            share = float(amount / supply) if supply else None
            holders.append(TokenHolder(raw.get("address", ""), amount, share))
        return holders
