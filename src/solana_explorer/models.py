"""Typed views over raw Solana RPC responses."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .constants import KNOWN_PROGRAMS, LAMPORTS_PER_SOL, SOL_DECIMALS

TOKEN_PROGRAMS = frozenset({"spl-token", "spl-token-2022"})
SYSTEM_TRANSFER_TYPES = frozenset({"transfer", "transferWithSeed"})
TOKEN_TRANSFER_TYPES = frozenset({"transfer", "transferChecked"})


def lamports_to_sol(lamports: int) -> Decimal:
    return Decimal(lamports) / LAMPORTS_PER_SOL


def scale_amount(raw: int, decimals: int) -> Decimal:
    return Decimal(raw).scaleb(-decimals)


def to_jsonable(value: Any) -> Any:
    """Recursively convert dataclasses, ``Decimal`` and ``datetime`` into JSON-safe values."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return value


def _timestamp(value: int | None) -> datetime | None:
    return datetime.fromtimestamp(value, tz=timezone.utc) if value is not None else None


# -- transactions -------------------------------------------------------------


@dataclass(frozen=True)
class AccountKey:
    pubkey: str
    signer: bool
    writable: bool


@dataclass(frozen=True)
class Instruction:
    """One executed instruction. ``position`` is ``"2"`` for top-level, ``"2.1"`` for inner."""

    position: str
    program_id: str
    program: str
    type: str | None
    inner: bool


@dataclass(frozen=True)
class Transfer:
    """A SOL or SPL token movement decoded from a parsed instruction."""

    asset: str  # "SOL" or "token"
    source: str
    destination: str
    amount: Decimal
    raw_amount: int
    decimals: int | None
    mint: str | None = None
    authority: str | None = None


@dataclass(frozen=True)
class SolBalanceChange:
    account: str
    pre_lamports: int
    post_lamports: int
    change_lamports: int


@dataclass(frozen=True)
class TokenBalanceChange:
    account: str
    owner: str | None
    mint: str
    decimals: int
    pre_amount: Decimal
    post_amount: Decimal
    change: Decimal


@dataclass(frozen=True)
class _TokenBalance:
    account: str
    owner: str | None
    mint: str
    decimals: int
    raw_amount: int


@dataclass(frozen=True)
class TransactionDetails:
    signature: str
    slot: int
    block_time: datetime | None
    success: bool
    error: Any
    fee_lamports: int
    compute_units: int | None
    version: str
    recent_blockhash: str | None
    accounts: tuple[AccountKey, ...]
    instructions: tuple[Instruction, ...]
    transfers: tuple[Transfer, ...]
    sol_changes: tuple[SolBalanceChange, ...]
    token_changes: tuple[TokenBalanceChange, ...]
    log_messages: tuple[str, ...]

    @property
    def signers(self) -> tuple[str, ...]:
        return tuple(key.pubkey for key in self.accounts if key.signer)

    @property
    def fee_payer(self) -> str | None:
        return self.accounts[0].pubkey if self.accounts else None

    @property
    def fee_sol(self) -> Decimal:
        return lamports_to_sol(self.fee_lamports)

    def to_dict(self) -> dict[str, Any]:
        data = to_jsonable(self)
        data["signers"] = list(self.signers)
        data["fee_sol"] = to_jsonable(self.fee_sol)
        return data

    @classmethod
    def from_rpc(cls, signature: str, raw: dict[str, Any]) -> TransactionDetails:
        """Build from a ``getTransaction`` result requested with ``jsonParsed`` encoding."""
        meta = raw.get("meta") or {}
        message = (raw.get("transaction") or {}).get("message") or {}
        accounts = _parse_account_keys(message.get("accountKeys") or [])
        addresses = [key.pubkey for key in accounts]

        pre_tokens = _parse_token_balances(meta.get("preTokenBalances"), addresses)
        post_tokens = _parse_token_balances(meta.get("postTokenBalances"), addresses)
        token_accounts = {b.account: b for b in (*pre_tokens.values(), *post_tokens.values())}

        instructions: list[Instruction] = []
        transfers: list[Transfer] = []
        for position, ix, inner in _iter_instructions(message, meta):
            instructions.append(_parse_instruction(position, ix, inner))
            transfer = _parse_transfer(ix, token_accounts)
            if transfer is not None:
                transfers.append(transfer)

        version = raw.get("version", "legacy")
        error = meta.get("err")
        return cls(
            signature=signature,
            slot=int(raw.get("slot", 0)),
            block_time=_timestamp(raw.get("blockTime")),
            success=error is None,
            error=error,
            fee_lamports=int(meta.get("fee", 0)),
            compute_units=meta.get("computeUnitsConsumed"),
            version=str(version),
            recent_blockhash=message.get("recentBlockhash"),
            accounts=accounts,
            instructions=tuple(instructions),
            transfers=tuple(transfers),
            sol_changes=_sol_changes(meta, addresses),
            token_changes=_token_changes(pre_tokens, post_tokens),
            log_messages=tuple(meta.get("logMessages") or ()),
        )


def _parse_account_keys(raw_keys: list[Any]) -> tuple[AccountKey, ...]:
    keys = []
    for item in raw_keys:
        if isinstance(item, str):
            keys.append(AccountKey(item, signer=False, writable=False))
        else:
            keys.append(
                AccountKey(
                    item.get("pubkey", ""),
                    signer=bool(item.get("signer")),
                    writable=bool(item.get("writable")),
                )
            )
    return tuple(keys)


def _iter_instructions(
    message: dict[str, Any], meta: dict[str, Any]
) -> Iterator[tuple[str, dict[str, Any], bool]]:
    """Yield instructions in execution order: each top-level one followed by its inner ones."""
    inner_by_index = {
        group.get("index"): group.get("instructions") or []
        for group in meta.get("innerInstructions") or []
    }
    for index, ix in enumerate(message.get("instructions") or []):
        yield str(index + 1), ix, False
        for inner_index, inner_ix in enumerate(inner_by_index.get(index, []), start=1):
            yield f"{index + 1}.{inner_index}", inner_ix, True


def _parse_instruction(position: str, ix: dict[str, Any], inner: bool) -> Instruction:
    program_id = ix.get("programId", "unknown")
    program = KNOWN_PROGRAMS.get(program_id) or ix.get("program") or program_id
    parsed = ix.get("parsed")
    ix_type = parsed.get("type") if isinstance(parsed, dict) else None
    return Instruction(position, program_id, program, ix_type, inner)


def _parse_transfer(
    ix: dict[str, Any], token_accounts: dict[str, _TokenBalance]
) -> Transfer | None:
    parsed = ix.get("parsed")
    if not isinstance(parsed, dict):
        return None
    program = ix.get("program")
    kind = parsed.get("type")
    info = parsed.get("info") or {}

    if program == "system" and kind in SYSTEM_TRANSFER_TYPES:
        lamports = int(info.get("lamports", 0))
        return Transfer(
            asset="SOL",
            source=info.get("source", ""),
            destination=info.get("destination", ""),
            amount=lamports_to_sol(lamports),
            raw_amount=lamports,
            decimals=SOL_DECIMALS,
        )

    if program in TOKEN_PROGRAMS and kind in TOKEN_TRANSFER_TYPES:
        source = info.get("source", "")
        destination = info.get("destination", "")
        mint: str | None
        decimals: int | None
        if kind == "transferChecked":
            token_amount = info.get("tokenAmount") or {}
            raw_amount = int(token_amount.get("amount", 0))
            decimals = token_amount.get("decimals")
            mint = info.get("mint")
        else:
            # Plain ``transfer`` omits the mint; recover it from the token balances.
            raw_amount = int(info.get("amount", 0))
            known = token_accounts.get(source) or token_accounts.get(destination)
            mint = known.mint if known else None
            decimals = known.decimals if known else None
        amount = scale_amount(raw_amount, decimals) if decimals is not None else Decimal(raw_amount)
        return Transfer(
            asset="token",
            source=source,
            destination=destination,
            amount=amount,
            raw_amount=raw_amount,
            decimals=decimals,
            mint=mint,
            authority=info.get("authority") or info.get("multisigAuthority"),
        )
    return None


def _parse_token_balances(
    entries: list[dict[str, Any]] | None, addresses: list[str]
) -> dict[int, _TokenBalance]:
    balances: dict[int, _TokenBalance] = {}
    for entry in entries or []:
        index = entry.get("accountIndex")
        if index is None:
            continue
        ui_amount = entry.get("uiTokenAmount") or {}
        balances[index] = _TokenBalance(
            account=addresses[index] if index < len(addresses) else f"#{index}",
            owner=entry.get("owner"),
            mint=entry.get("mint", ""),
            decimals=int(ui_amount.get("decimals", 0)),
            raw_amount=int(ui_amount.get("amount", 0)),
        )
    return balances


def _sol_changes(meta: dict[str, Any], addresses: list[str]) -> tuple[SolBalanceChange, ...]:
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    changes = []
    for index, (before, after) in enumerate(zip(pre, post, strict=False)):
        if before != after:
            account = addresses[index] if index < len(addresses) else f"#{index}"
            changes.append(SolBalanceChange(account, before, after, after - before))
    return tuple(changes)


def _token_changes(
    pre: dict[int, _TokenBalance], post: dict[int, _TokenBalance]
) -> tuple[TokenBalanceChange, ...]:
    changes = []
    for index in sorted(pre.keys() | post.keys()):
        before, after = pre.get(index), post.get(index)
        reference = after or before
        assert reference is not None
        pre_raw = before.raw_amount if before else 0
        post_raw = after.raw_amount if after else 0
        if pre_raw == post_raw:
            continue
        decimals = reference.decimals
        changes.append(
            TokenBalanceChange(
                account=reference.account,
                owner=reference.owner,
                mint=reference.mint,
                decimals=decimals,
                pre_amount=scale_amount(pre_raw, decimals),
                post_amount=scale_amount(post_raw, decimals),
                change=scale_amount(post_raw - pre_raw, decimals),
            )
        )
    return tuple(changes)


# -- accounts -----------------------------------------------------------------


@dataclass(frozen=True)
class AccountSummary:
    address: str
    exists: bool
    lamports: int
    owner: str | None
    executable: bool
    space: int | None
    program: str | None  # parser name reported by the node, e.g. "spl-token"
    account_type: str | None  # e.g. "account", "mint"
    parsed: dict[str, Any] | None

    @property
    def balance_sol(self) -> Decimal:
        return lamports_to_sol(self.lamports)

    def to_dict(self) -> dict[str, Any]:
        data = to_jsonable(self)
        data["balance_sol"] = to_jsonable(self.balance_sol)
        return data

    @classmethod
    def from_rpc(cls, address: str, value: dict[str, Any] | None) -> AccountSummary:
        """Build from the ``value`` field of a ``getAccountInfo`` (``jsonParsed``) result."""
        if value is None:
            return cls(address, False, 0, None, False, None, None, None, None)
        data = value.get("data")
        program = account_type = None
        parsed: dict[str, Any] | None = None
        if isinstance(data, dict):
            program = data.get("program")
            if isinstance(data.get("parsed"), dict):
                parsed = data["parsed"]
                account_type = parsed.get("type")
        return cls(
            address=address,
            exists=True,
            lamports=int(value.get("lamports", 0)),
            owner=value.get("owner"),
            executable=bool(value.get("executable")),
            space=value.get("space"),
            program=program,
            account_type=account_type,
            parsed=parsed,
        )


@dataclass(frozen=True)
class SignatureInfo:
    signature: str
    slot: int
    block_time: datetime | None
    success: bool
    error: Any
    memo: str | None
    confirmation_status: str | None

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)

    @classmethod
    def from_rpc(cls, raw: dict[str, Any]) -> SignatureInfo:
        return cls(
            signature=raw.get("signature", ""),
            slot=int(raw.get("slot", 0)),
            block_time=_timestamp(raw.get("blockTime")),
            success=raw.get("err") is None,
            error=raw.get("err"),
            memo=raw.get("memo"),
            confirmation_status=raw.get("confirmationStatus"),
        )
