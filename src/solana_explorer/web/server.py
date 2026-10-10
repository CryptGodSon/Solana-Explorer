"""Local web server: a JSON API over :class:`SolanaExplorer` plus a single-page UI.

Only the standard library is used. The server binds to localhost by default and
rejects requests whose ``Host`` header is not a loopback name, which protects
against DNS-rebinding attacks from other websites open in the browser.
"""

from __future__ import annotations

import json
import mimetypes
import re
import threading
import webbrowser
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .._version import __version__
from ..constants import CLUSTERS, KNOWN_PROGRAMS, KNOWN_TOKENS
from ..errors import (
    InvalidInputError,
    NotFoundError,
    RpcTransportError,
    SolanaExplorerError,
)
from ..explorer import SolanaExplorer, account_kind
from ..models import lamports_to_sol, to_jsonable
from ..rpc import cluster_name, resolve_endpoint
from ..validation import is_valid_address, is_valid_signature

ExplorerFactory = Callable[[str], SolanaExplorer]

STATIC_PACKAGE = "solana_explorer.web"
STATIC_FILES = ("index.html", "app.js", "style.css", "favicon.svg")
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
CUSTOM_CLUSTER = "custom"
HISTORY_PAGE_SIZE = 25

CSP = (
    "default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline' "
    "https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
)


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _status_for(error: SolanaExplorerError) -> HTTPStatus:
    if isinstance(error, InvalidInputError):
        return HTTPStatus.BAD_REQUEST
    if isinstance(error, NotFoundError):
        return HTTPStatus.NOT_FOUND
    if isinstance(error, RpcTransportError) and "429" in str(error):
        return HTTPStatus.TOO_MANY_REQUESTS
    return HTTPStatus.BAD_GATEWAY


class ExplorerWebApp:
    """Routes API requests to one :class:`SolanaExplorer` per cluster."""

    def __init__(
        self, endpoint: str | None = None, explorer_factory: ExplorerFactory | None = None
    ) -> None:
        resolved = resolve_endpoint(endpoint)
        self.endpoints: dict[str, str] = dict(CLUSTERS)
        self.default_cluster = cluster_name(resolved) or CUSTOM_CLUSTER
        if self.default_cluster == CUSTOM_CLUSTER:
            self.endpoints[CUSTOM_CLUSTER] = resolved
        self._factory = explorer_factory or (lambda url: SolanaExplorer(endpoint=url, timeout=20))
        self._explorers: dict[str, SolanaExplorer] = {}
        self._lock = threading.Lock()
        self._routes: list[tuple[re.Pattern[str], Callable[..., Any]]] = [
            (re.compile(r"/api/config"), self.config),
            (re.compile(r"/api/network"), self.network),
            (re.compile(r"/api/supply"), self.supply),
            (re.compile(r"/api/blocks/latest"), self.latest_blocks),
            (re.compile(r"/api/transactions/latest"), self.latest_transactions),
            (re.compile(r"/api/search"), self.search),
            (re.compile(r"/api/tx/([^/]+)"), self.transaction),
            (re.compile(r"/api/block/(\d+)"), self.block),
            (re.compile(r"/api/account/([^/]+)"), self.account),
            (re.compile(r"/api/account/([^/]+)/tokens"), self.account_tokens),
            (re.compile(r"/api/account/([^/]+)/transactions"), self.account_transactions),
            (re.compile(r"/api/token/([^/]+)/holders"), self.token_holders),
        ]

    # -- plumbing ----------------------------------------------------------

    def explorer(self, query: dict[str, list[str]]) -> SolanaExplorer:
        cluster = (query.get("cluster") or [self.default_cluster])[0]
        if cluster not in self.endpoints:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"Unknown cluster {cluster!r}")
        with self._lock:
            if cluster not in self._explorers:
                self._explorers[cluster] = self._factory(self.endpoints[cluster])
            return self._explorers[cluster]

    def handle(self, path: str, query: dict[str, list[str]]) -> tuple[HTTPStatus, Any]:
        for pattern, handler in self._routes:
            match = pattern.fullmatch(path)
            if match:
                try:
                    return HTTPStatus.OK, handler(query, *match.groups())
                except ApiError as exc:
                    return exc.status, {"error": exc.message}
                except SolanaExplorerError as exc:
                    return _status_for(exc), {"error": str(exc)}
        return HTTPStatus.NOT_FOUND, {"error": "Unknown API endpoint"}

    # -- endpoints ---------------------------------------------------------

    def config(self, query: dict[str, list[str]]) -> dict[str, Any]:
        return {
            "version": __version__,
            "default_cluster": self.default_cluster,
            "clusters": list(self.endpoints),
            "labels": {**KNOWN_PROGRAMS, **KNOWN_TOKENS},
        }

    def network(self, query: dict[str, list[str]]) -> dict[str, Any]:
        return self.explorer(query).network_stats().to_dict()

    def supply(self, query: dict[str, list[str]]) -> dict[str, Any]:
        # Lamport totals exceed JavaScript's safe integer range, so send SOL strings.
        value = self.explorer(query).supply()
        return {
            f"{key}_sol": to_jsonable(lamports_to_sol(value[key])) if key in value else None
            for key in ("total", "circulating", "nonCirculating")
        }

    def latest_blocks(self, query: dict[str, list[str]]) -> list[dict[str, Any]]:
        limit = _int_param(query, "limit", 8, 1, 20)
        return [block.to_dict() for block in self.explorer(query).latest_blocks(limit)]

    def latest_transactions(self, query: dict[str, list[str]]) -> dict[str, Any]:
        limit = _int_param(query, "limit", 12, 1, 50)
        block, transactions = self.explorer(query).latest_transactions(limit)
        return {
            "slot": block.slot,
            "block_time": to_jsonable(block.block_time),
            "transactions": [to_jsonable(tx) for tx in transactions],
        }

    def search(self, query: dict[str, list[str]]) -> dict[str, str]:
        term = (query.get("q") or [""])[0].strip()
        if term.isdigit():
            return {"type": "block", "value": term}
        if is_valid_signature(term):
            return {"type": "tx", "value": term}
        if is_valid_address(term):
            return {"type": "account", "value": term}
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "Enter a transaction signature, an account or token address, or a block number",
        )

    def transaction(self, query: dict[str, list[str]], signature: str) -> dict[str, Any]:
        explorer = self.explorer(query)
        tx = explorer.get_transaction(signature)
        mints = {t.mint for t in tx.transfers if t.mint} | {c.mint for c in tx.token_changes}
        data = tx.to_dict()
        data["tokens"] = _token_labels(explorer, mints)
        return data

    def block(self, query: dict[str, list[str]], slot: str) -> dict[str, Any]:
        return self.explorer(query).get_block(int(slot)).to_dict()

    def account(self, query: dict[str, list[str]], address: str) -> dict[str, Any]:
        explorer = self.explorer(query)
        account = explorer.get_account(address)
        kind = account_kind(account)
        data: dict[str, Any] = {"account": account.to_dict(), "kind": kind}
        if kind == "token_mint":
            data["token"] = explorer.get_token_info(account).to_dict()
        elif kind == "token_account":
            info = (account.parsed or {}).get("info") or {}
            mint = info.get("mint")
            data["token_account"] = {
                "mint": mint,
                "owner": info.get("owner"),
                "amount": (info.get("tokenAmount") or {}).get("uiAmountString"),
                "state": info.get("state"),
                "token": _token_labels(explorer, {mint}).get(mint) if mint else None,
            }
        return data

    def account_tokens(self, query: dict[str, list[str]], address: str) -> list[dict[str, Any]]:
        return [h.to_dict() for h in self.explorer(query).get_token_holdings(address)]

    def account_transactions(
        self, query: dict[str, list[str]], address: str
    ) -> list[dict[str, Any]]:
        limit = _int_param(query, "limit", HISTORY_PAGE_SIZE, 1, 100)
        before = (query.get("before") or [None])[0]
        signatures = self.explorer(query).get_signatures(address, limit=limit, before=before)
        return [s.to_dict() for s in signatures]

    def token_holders(self, query: dict[str, list[str]], mint: str) -> list[dict[str, Any]]:
        explorer = self.explorer(query)
        info = explorer.get_token_info(explorer.get_account(mint))
        return [h.to_dict() for h in explorer.get_top_holders(mint, info.supply)]


def _int_param(query: dict[str, list[str]], name: str, default: int, low: int, high: int) -> int:
    raw = (query.get(name) or [None])[0]
    if raw is None:
        return default
    try:
        return max(low, min(high, int(raw)))
    except ValueError:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{name} must be an integer") from None


def _token_labels(explorer: SolanaExplorer, mints: set[str]) -> dict[str, dict[str, Any]]:
    try:
        metadata = explorer.token_metadata(sorted(m for m in mints if m))
    except SolanaExplorerError:
        return {}
    return {mint: {"name": md.name, "symbol": md.symbol} for mint, md in metadata.items()}


def _static_file(name: str) -> bytes:
    return resources.files(STATIC_PACKAGE).joinpath("static", name).read_bytes()


def make_handler(app: ExplorerWebApp, *, quiet: bool = True) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"solana-explorer/{__version__}"

        def do_GET(self) -> None:
            if not self._host_allowed():
                self._send(HTTPStatus.FORBIDDEN, b"Forbidden host", "text/plain")
                return
            url = urlsplit(self.path)
            if url.path.startswith("/api/"):
                status, payload = app.handle(url.path, parse_qs(url.query))
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._send(status, body, "application/json; charset=utf-8", cache=False)
            elif url.path.startswith("/static/") and url.path[8:] in STATIC_FILES:
                name = url.path[8:]
                content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
                if content_type.startswith("text/") or name.endswith(".js"):
                    content_type += "; charset=utf-8"
                self._send(HTTPStatus.OK, _static_file(name), content_type, cache=False)
            else:
                # Client-side routes (/tx/..., /account/..., /block/...) all load the app.
                body = _static_file("index.html")
                self._send(HTTPStatus.OK, body, "text/html; charset=utf-8", cache=False)

        def _host_allowed(self) -> bool:
            if self.server.server_address[0] not in ("127.0.0.1", "::1", "localhost"):
                return True  # explicitly exposed with --host; the user opted in
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return host in LOOPBACK_HOSTS

        def _send(
            self, status: HTTPStatus, body: bytes, content_type: str, *, cache: bool = True
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "max-age=300" if cache else "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            if not quiet:
                super().log_message(format, *args)

    return Handler


def create_server(
    app: ExplorerWebApp, host: str = "127.0.0.1", port: int = 8000, *, quiet: bool = True
) -> ThreadingHTTPServer:
    handler = make_handler(app, quiet=quiet)
    try:
        server = ThreadingHTTPServer((host, port), handler)
    except OSError:
        if port == 0:
            raise
        server = ThreadingHTTPServer((host, 0), handler)  # port busy: pick a free one
    server.daemon_threads = True
    return server


def serve(
    endpoint: str | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    open_browser: bool = True,
    quiet: bool = True,
) -> None:
    app = ExplorerWebApp(endpoint)
    server = create_server(app, host, port, quiet=quiet)
    bound_port = server.server_address[1]
    # Use the literal IP: on Windows "localhost" tries IPv6 first, adding ~200 ms per request.
    display_host = "127.0.0.1" if host in ("0.0.0.0", "localhost") else host
    if ":" in display_host:
        display_host = f"[{display_host}]"
    url = f"http://{display_host}:{bound_port}/"
    print(f"Solana Explorer running at {url}  (cluster: {app.default_cluster})", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
