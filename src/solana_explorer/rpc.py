"""Minimal Solana JSON-RPC client built on :mod:`requests`."""

from __future__ import annotations

import contextlib
import itertools
import os
import time
from typing import Any

import requests

from ._version import __version__
from .constants import CLUSTER_ALIASES, CLUSTERS, DEFAULT_CLUSTER, RPC_URL_ENV_VAR
from .errors import InvalidInputError, RpcError, RpcTransportError

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
DEFAULT_COMMITMENT = "confirmed"
_MAX_RETRY_AFTER = 30.0


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
        self._session = session or requests.Session()
        self._session.headers.setdefault("User-Agent", f"solana-explorer/{__version__}")
        self._ids = itertools.count(1)

    def close(self) -> None:
        self._session.close()

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

    def get_transaction(self, signature: str) -> dict[str, Any] | None:
        """``getTransaction`` with ``jsonParsed`` encoding (``None`` when not found)."""
        return self.call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": self.commitment,
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )

    def get_account_info(self, address: str) -> dict[str, Any] | None:
        """``getAccountInfo`` with ``jsonParsed`` encoding (``None`` when the account is empty)."""
        result = self.call(
            "getAccountInfo",
            [address, {"encoding": "jsonParsed", "commitment": self.commitment}],
        )
        return result.get("value") if isinstance(result, dict) else None

    def get_signatures_for_address(
        self, address: str, *, limit: int = 10, before: str | None = None
    ) -> list[dict[str, Any]]:
        """``getSignaturesForAddress``: newest signatures first."""
        options: dict[str, Any] = {"limit": limit, "commitment": self.commitment}
        if before:
            options["before"] = before
        return self.call("getSignaturesForAddress", [address, options]) or []
