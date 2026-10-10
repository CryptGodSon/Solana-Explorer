"""Minimal Solana JSON-RPC client built on :mod:`requests`."""

from __future__ import annotations

import contextlib
import itertools
import os
import re
import threading
import time
from typing import Any

import requests

from ._version import __version__
from .constants import CLUSTER_ALIASES, CLUSTERS, DEFAULT_CLUSTER, RPC_URL_ENV_VAR
from .errors import InvalidInputError, RpcError, RpcTransportError

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
DEFAULT_COMMITMENT = "confirmed"
_MAX_RETRY_AFTER = 10.0

TRANSACTION_VERSION_NOT_SUPPORTED = -32015
_VERSION_HINT = re.compile(r'"?maxSupportedTransactionVersion"?\s*:\s*(\d+)')


def resolve_endpoint(cluster_or_url: str | None = None) -> str:
    """Turn a cluster name, an RPC URL or ``None`` into an RPC URL.

    ``None`` falls back to the ``SOLANA_RPC_URL`` environment variable and then
    to the public mainnet-beta endpoint.
    """
    if not cluster_or_url:
        return os.environ.get(RPC_URL_ENV_VAR) or CLUSTERS[DEFAULT_CLUSTER]
    value = cluster_or_url.strip()
    if value.startswith(("http://", "https://")):
        return value
    name = CLUSTER_ALIASES.get(value.lower(), value.lower())
    if name in CLUSTERS:
        return CLUSTERS[name]
    choices = ", ".join(CLUSTERS)
    raise InvalidInputError(f"Unknown cluster {value!r}; use one of: {choices} or an http(s) URL")


def _required_transaction_version(error: RpcError) -> int | None:
    """Extract the version a node asks for in a "Transaction version not supported" error."""
    if error.code != TRANSACTION_VERSION_NOT_SUPPORTED:
        return None
    match = _VERSION_HINT.search(error.message)
    return int(match.group(1)) if match else None


def cluster_name(endpoint: str) -> str | None:
    """Return the public cluster name for *endpoint*, or ``None`` for custom RPC nodes."""
    normalized = endpoint.rstrip("/")
    for name, url in CLUSTERS.items():
        if url == normalized:
            return name
    return None


class SolanaRpcClient:
    """Thin JSON-RPC 2.0 client with timeouts and retry/backoff on transient failures."""

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        timeout: float = 15.0,
        max_retries: int = 3,
        backoff: float = 0.5,
        commitment: str = DEFAULT_COMMITMENT,
        session: requests.Session | None = None,
    ) -> None:
        self.endpoint = resolve_endpoint(endpoint)
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.backoff = max(0.0, backoff)
        self.commitment = commitment
        self.max_transaction_version = 0
        self._shared_session = session
        self._local = threading.local()
        self._sessions: list[requests.Session] = []
        self._sessions_lock = threading.Lock()
        self._ids = itertools.count(1)

    @property
    def _session(self) -> requests.Session:
        """The injected session, or one ``requests.Session`` per thread (not thread-safe)."""
        if self._shared_session is not None:
            return self._shared_session
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers["User-Agent"] = f"solana-explorer/{__version__}"
            self._local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    def close(self) -> None:
        if self._shared_session is not None:
            self._shared_session.close()
        with self._sessions_lock:
            for session in self._sessions:
                session.close()
            self._sessions.clear()

    def __enter__(self) -> SolanaRpcClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        """Execute a JSON-RPC request and return its ``result`` field."""
        payload = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": params or [],
        }
        attempt = 0
        while True:
            try:
                response = self._session.post(self.endpoint, json=payload, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt < self.max_retries:
                    self._wait(attempt)
                    attempt += 1
                    continue
                raise RpcTransportError(f"Could not reach {self.endpoint}: {exc}") from exc

            if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                self._wait(attempt, response.headers.get("Retry-After"))
                attempt += 1
                continue
            return self._parse(response)

    def _wait(self, attempt: int, retry_after: str | None = None) -> None:
        delay = self.backoff * (2**attempt)
        if retry_after:
            with contextlib.suppress(ValueError):
                delay = min(float(retry_after), _MAX_RETRY_AFTER)
        if delay > 0:
            time.sleep(delay)

    def _parse(self, response: requests.Response) -> Any:
        if response.status_code == 429:
            raise RpcTransportError(
                f"Rate limited by {self.endpoint} (HTTP 429). Try again later or use "
                f"a dedicated RPC node via --rpc-url / ${RPC_URL_ENV_VAR}."
            )
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict) and body.get("error"):
            error = body["error"]
            if isinstance(error, dict):
                raise RpcError(error.get("code"), str(error.get("message", "")), error.get("data"))
            raise RpcError(None, str(error))
        if not response.ok:
            raise RpcTransportError(
                f"HTTP {response.status_code} from {self.endpoint}: {response.text[:200]}"
            )
        if not isinstance(body, dict) or "result" not in body:
            raise RpcTransportError(
                f"Unexpected response from {self.endpoint}: {response.text[:200]}"
            )
        return body["result"]

    # -- typed helpers -----------------------------------------------------

    def _call_versioned(self, method: str, target: Any, options: dict[str, Any]) -> Any:
        """Call a method that takes ``maxSupportedTransactionVersion``.

        If the node reports that the data uses a newer transaction version than we
        advertised, the request is retried once with the version it asks for, and
        that version is remembered for later calls.
        """

        def attempt() -> Any:
            params = {**options, "maxSupportedTransactionVersion": self.max_transaction_version}
            return self.call(method, [target, params])

        try:
            return attempt()
        except RpcError as exc:
            required = _required_transaction_version(exc)
            if required is None or required <= self.max_transaction_version:
                raise
            self.max_transaction_version = required
            return attempt()

    def get_transaction(self, signature: str) -> dict[str, Any] | None:
        """``getTransaction`` with ``jsonParsed`` encoding (``None`` when not found)."""
        return self._call_versioned(
            "getTransaction", signature, {"encoding": "jsonParsed", "commitment": self.commitment}
        )

    def get_block(
        self, slot: int, *, transaction_details: str = "signatures", rewards: bool = True
    ) -> dict[str, Any] | None:
        """``getBlock``; details: ``signatures``, ``accounts``, ``full`` or ``none``."""
        return self._call_versioned(
            "getBlock",
            slot,
            {
                "encoding": "jsonParsed",
                "transactionDetails": transaction_details,
                "rewards": rewards,
                "commitment": self.commitment,
            },
        )

    def get_blocks(self, start_slot: int, end_slot: int) -> list[int]:
        """Slots between ``start_slot`` and ``end_slot`` (inclusive) that produced a block."""
        return self.call("getBlocks", [start_slot, end_slot, {"commitment": self.commitment}]) or []

    def get_slot(self) -> int:
        return int(self.call("getSlot", [{"commitment": self.commitment}]))

    def get_epoch_info(self) -> dict[str, Any]:
        return self.call("getEpochInfo", [{"commitment": self.commitment}])

    def get_recent_performance_samples(self, limit: int = 30) -> list[dict[str, Any]]:
        return self.call("getRecentPerformanceSamples", [limit]) or []

    def get_supply(self) -> dict[str, Any]:
        result = self.call(
            "getSupply",
            [{"commitment": self.commitment, "excludeNonCirculatingAccountsList": True}],
        )
        return result.get("value") or {}

    def get_version(self) -> dict[str, Any]:
        return self.call("getVersion")

    def get_account_info(self, address: str) -> dict[str, Any] | None:
        """``getAccountInfo`` with ``jsonParsed`` encoding (``None`` when the account is empty)."""
        result = self.call(
            "getAccountInfo",
            [address, {"encoding": "jsonParsed", "commitment": self.commitment}],
        )
        return result.get("value") if isinstance(result, dict) else None

    def get_multiple_accounts(
        self, addresses: list[str], *, encoding: str = "base64"
    ) -> list[dict[str, Any] | None]:
        """``getMultipleAccounts`` (the node accepts at most 100 addresses per call)."""
        result = self.call(
            "getMultipleAccounts",
            [addresses, {"encoding": encoding, "commitment": self.commitment}],
        )
        return result.get("value") or []

    def get_token_accounts_by_owner(self, owner: str, program_id: str) -> list[dict[str, Any]]:
        result = self.call(
            "getTokenAccountsByOwner",
            [
                owner,
                {"programId": program_id},
                {"encoding": "jsonParsed", "commitment": self.commitment},
            ],
        )
        return result.get("value") or []

    def get_token_largest_accounts(self, mint: str) -> list[dict[str, Any]]:
        result = self.call("getTokenLargestAccounts", [mint, {"commitment": self.commitment}])
        return result.get("value") or []

    def get_signatures_for_address(
        self, address: str, *, limit: int = 10, before: str | None = None
    ) -> list[dict[str, Any]]:
        """``getSignaturesForAddress``: newest signatures first."""
        options: dict[str, Any] = {"limit": limit, "commitment": self.commitment}
        if before:
            options["before"] = before
        return self.call("getSignaturesForAddress", [address, options]) or []
