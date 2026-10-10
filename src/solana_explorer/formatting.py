"""Plain-text rendering shared by the CLI and the GUI."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlencode

from .constants import KNOWN_PROGRAMS, KNOWN_TOKENS
from .models import (
    AccountSummary,
    SignatureInfo,
    TransactionDetails,
    lamports_to_sol,
    scale_amount,
)
from .rpc import cluster_name

EXPLORER_BASE_URL = "https://explorer.solana.com"
_LABEL_WIDTH = 16


def format_amount(value: Decimal, *, signed: bool = False) -> str:
    """Render a decimal without trailing zeros and with thousands separators."""
    spec = "+,f" if signed else ",f"
    return format(value.normalize(), spec)


def format_sol(lamports: int, *, signed: bool = False) -> str:
    return f"{format_amount(lamports_to_sol(lamports), signed=signed)} SOL"


def format_time(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S UTC") if value else "unknown"


def label(address: str | None) -> str:
    """Return the address followed by a friendly name when it is a well-known program or mint."""
    if not address:
        return "unknown"
    name = KNOWN_PROGRAMS.get(address) or KNOWN_TOKENS.get(address)
    return f"{address} ({name})" if name else address


def token_symbol(mint: str | None) -> str:
    if not mint:
        return "tokens (unknown mint)"
    return KNOWN_TOKENS.get(mint, mint)


def explorer_url(kind: str, value: str, endpoint: str) -> str:
    """Link to explorer.solana.com for a ``tx`` or ``address`` on the given RPC endpoint."""
    name = cluster_name(endpoint)
    if name == "mainnet-beta":
        query = ""
    elif name:
        query = "?" + urlencode({"cluster": name})
    else:
        query = "?" + urlencode({"cluster": "custom", "customUrl": endpoint})
    return f"{EXPLORER_BASE_URL}/{kind}/{value}{query}"


def _row(name: str, value: object) -> str:
    return f"  {name + ':':<{_LABEL_WIDTH}} {value}"


def _section(title: str, lines: Sequence[str], count: int | None = None) -> list[str]:
    if not lines:
        return []
    return ["", f"{title} ({len(lines) if count is None else count})", *lines]


def format_error(error: object) -> str:
    return error if isinstance(error, str) else json.dumps(error, separators=(",", ":"))


def format_transaction(tx: TransactionDetails, endpoint: str, *, show_logs: bool = False) -> str:
    status = "Success" if tx.success else f"FAILED - {format_error(tx.error)}"
    lines = [
        f"Transaction {tx.signature}",
        _row("Status", status),
        _row("Slot", f"{tx.slot:,}"),
        _row("Block time", format_time(tx.block_time)),
        _row("Fee", format_sol(tx.fee_lamports)),
    ]
    if tx.compute_units is not None:
        lines.append(_row("Compute units", f"{tx.compute_units:,}"))
    lines += [
        _row("Version", tx.version),
        _row("Fee payer", tx.fee_payer or "unknown"),
    ]
    other_signers = [s for s in tx.signers if s != tx.fee_payer]
    if other_signers:
        lines.append(_row("Other signers", ", ".join(other_signers)))
    lines.append(_row("Explorer", explorer_url("tx", tx.signature, endpoint)))

    transfers = []
    for t in tx.transfers:
        unit = "SOL" if t.asset == "SOL" else token_symbol(t.mint)
        if t.asset != "SOL" and t.decimals is None:
            unit = f"raw units of {unit}"
        transfers.append(f"  {format_amount(t.amount)} {unit}")
        transfers.append(f"      {t.source} -> {t.destination}")
    lines += _section("Transfers", transfers, count=len(tx.transfers))

    lines += _section(
        "SOL balance changes",
        [
            f"  {format_sol(c.change_lamports, signed=True):>22}  {c.account}"
            for c in tx.sol_changes
        ],
    )
    lines += _section(
        "Token balance changes",
        [
            f"  {format_amount(c.change, signed=True):>22}  {token_symbol(c.mint)}"
            f"  account {c.account}" + (f" (owner {c.owner})" if c.owner else "")
            for c in tx.token_changes
        ],
    )
    lines += _section(
        "Instructions",
        [
            f"  {'  ' if ix.inner else ''}#{ix.position:<5} {ix.program}"
            + (f": {ix.type}" if ix.type else "")
            for ix in tx.instructions
        ],
    )
    if show_logs:
        lines += _section("Log messages", [f"  {line}" for line in tx.log_messages])
    return "\n".join(lines)


def _token_supply(mint_info: dict) -> str:
    try:
        return format_amount(scale_amount(int(mint_info["supply"]), int(mint_info["decimals"])))
    except (KeyError, TypeError, ValueError):
        return "unknown"


def format_account(account: AccountSummary, endpoint: str) -> str:
    lines = [f"Account {account.address}"]
    if not account.exists:
        lines += [
            _row("Status", "Not found (no lamports and no data on this cluster)"),
            _row("Explorer", explorer_url("address", account.address, endpoint)),
        ]
        return "\n".join(lines)
    lines += [
        _row("Balance", format_sol(account.lamports)),
        _row("Owner program", label(account.owner)),
        _row("Executable", "yes" if account.executable else "no"),
    ]
    if account.space is not None:
        lines.append(_row("Data size", f"{account.space:,} bytes"))
    if account.program:
        kind = f"{account.program} {account.account_type or ''}".strip()
        lines.append(_row("Account type", kind))
    info = (account.parsed or {}).get("info") or {}
    if account.account_type == "account" and "mint" in info:
        amount = (info.get("tokenAmount") or {}).get("uiAmountString", "?")
        lines += [
            _row("Token mint", label(info["mint"])),
            _row("Token balance", amount),
            _row("Token owner", info.get("owner", "unknown")),
        ]
    elif account.account_type == "mint":
        lines += [
            _row("Decimals", info.get("decimals", "?")),
            _row("Supply", _token_supply(info)),
            _row("Mint authority", info.get("mintAuthority") or "none"),
        ]
    lines.append(_row("Explorer", explorer_url("address", account.address, endpoint)))
    return "\n".join(lines)


def format_signatures(address: str, signatures: Sequence[SignatureInfo]) -> str:
    if not signatures:
        return f"No transactions found for {address}"
    lines = [f"Recent transactions for {address} ({len(signatures)})"]
    for sig in signatures:
        status = "ok    " if sig.success else "FAILED"
        lines.append(f"  {format_time(sig.block_time)}  {status}  {sig.signature}")
        if sig.memo:
            lines.append(f"      memo: {sig.memo}")
    return "\n".join(lines)
