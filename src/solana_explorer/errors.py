"""Exception hierarchy used across the package."""

from __future__ import annotations

from typing import Any


class SolanaExplorerError(Exception):
    """Base class for every error raised by ``solana_explorer``."""


class InvalidInputError(SolanaExplorerError, ValueError):
    """Raised when a signature, address or option value is malformed."""


class RpcTransportError(SolanaExplorerError):
    """Raised when the RPC node cannot be reached or returns a non-JSON-RPC response."""


class RpcError(SolanaExplorerError):
    """Raised when the RPC node answers with a JSON-RPC error object."""

    def __init__(self, code: int | None, message: str, data: Any = None) -> None:
        super().__init__(f"RPC error {code}: {message}" if code is not None else message)
        self.code = code
        self.message = message
        self.data = data


class TransactionNotFoundError(SolanaExplorerError):
    """Raised when the node has no record of the requested transaction."""

    def __init__(self, signature: str) -> None:
        super().__init__(
            f"Transaction {signature} was not found (it may be too old for this node, "
            "not yet confirmed, or on a different cluster)"
        )
        self.signature = signature
