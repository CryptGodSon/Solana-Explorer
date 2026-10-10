"""High-level API: validated, cached lookups that return typed models."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol

from .cache import LRUCache
from .errors import SolanaExplorerError, TransactionNotFoundError
from .models import AccountSummary, SignatureInfo, TransactionDetails
from .rpc import SolanaRpcClient
from .validation import split_identifiers, validate_address, validate_signature

MAX_HISTORY_LIMIT = 1000


class RpcClient(Protocol):
    endpoint: str

    def get_transaction(self, signature: str) -> dict[str, Any] | None: ...

    def get_account_info(self, address: str) -> dict[str, Any] | None: ...

    def get_signatures_for_address(
        self, address: str, *, limit: int = 10, before: str | None = None
    ) -> list[dict[str, Any]]: ...


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
        self._transactions.clear()
