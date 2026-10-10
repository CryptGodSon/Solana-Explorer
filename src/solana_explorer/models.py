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
    info: Any = None  # decoded arguments when the node can parse the program
    accounts: tuple[str, ...] = ()  # account list for instructions the node cannot parse
    data: str | None = None  # base58 instruction data for unparsed instructions


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
    if isinstance(parsed, dict):
        ix_type, info = parsed.get("type"), parsed.get("info")
    else:
        # e.g. the Memo program, whose "parsed" value is the memo text itself
        ix_type, info = None, parsed
    return Instruction(
        position,
        program_id,
        program,
        ix_type,
        inner,
        info=info,
        accounts=tuple(ix.get("accounts") or ()),
        data=ix.get("data"),
    )


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


# -- blocks -------------------------------------------------------------------

VOTE_PROGRAM_ID = "Vote111111111111111111111111111111111111111"


@dataclass(frozen=True)
class BlockTransaction:
    signature: str
    success: bool
    fee_lamports: int
    signer: str | None
    is_vote: bool

    @classmethod
    def from_rpc(cls, raw: dict[str, Any]) -> BlockTransaction:
        """Build from one entry of a ``getBlock`` result with ``transactionDetails=accounts``."""
        tx = raw.get("transaction") or {}
        meta = raw.get("meta") or {}
        keys = _parse_account_keys(tx.get("accountKeys") or [])
        signatures = tx.get("signatures") or [""]
        return cls(
            signature=signatures[0],
            success=meta.get("err") is None,
            fee_lamports=int(meta.get("fee", 0)),
            signer=keys[0].pubkey if keys else None,
            is_vote=any(key.pubkey == VOTE_PROGRAM_ID for key in keys),
        )


@dataclass(frozen=True)
class BlockSummary:
    slot: int
    blockhash: str
    previous_blockhash: str | None
    parent_slot: int | None
    block_time: datetime | None
    block_height: int | None
    transaction_count: int
    leader: str | None
    leader_reward_lamports: int
    transactions: tuple[BlockTransaction, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = to_jsonable(self)
        data["leader_reward_sol"] = to_jsonable(lamports_to_sol(self.leader_reward_lamports))
        data["vote_transaction_count"] = sum(1 for tx in self.transactions if tx.is_vote)
        return data

    @classmethod
    def from_rpc(cls, slot: int, raw: dict[str, Any]) -> BlockSummary:
        """Build from ``getBlock`` with ``signatures`` or ``accounts`` transaction details."""
        transactions = tuple(BlockTransaction.from_rpc(tx) for tx in raw.get("transactions") or [])
        count = len(transactions) if "transactions" in raw else len(raw.get("signatures") or [])
        fee_rewards = [r for r in raw.get("rewards") or [] if r.get("rewardType") == "Fee"]
        return cls(
            slot=slot,
            blockhash=raw.get("blockhash", ""),
            previous_blockhash=raw.get("previousBlockhash"),
            parent_slot=raw.get("parentSlot"),
            block_time=_timestamp(raw.get("blockTime")),
            block_height=raw.get("blockHeight"),
            transaction_count=count,
            leader=fee_rewards[0].get("pubkey") if fee_rewards else None,
            leader_reward_lamports=sum(int(r.get("lamports", 0)) for r in fee_rewards),
            transactions=transactions,
        )


# -- network ------------------------------------------------------------------


@dataclass(frozen=True)
class PerformanceSample:
    slot: int
    num_transactions: int
    num_non_vote_transactions: int | None
    num_slots: int
    period_secs: int

    @property
    def tps(self) -> float:
        return self.num_transactions / self.period_secs if self.period_secs else 0.0

    @property
    def non_vote_tps(self) -> float | None:
        if self.num_non_vote_transactions is None or not self.period_secs:
            return None
        return self.num_non_vote_transactions / self.period_secs

    @classmethod
    def from_rpc(cls, raw: dict[str, Any]) -> PerformanceSample:
        return cls(
            slot=int(raw.get("slot", 0)),
            num_transactions=int(raw.get("numTransactions", 0)),
            num_non_vote_transactions=raw.get("numNonVoteTransactions"),
            num_slots=int(raw.get("numSlots", 0)),
            period_secs=int(raw.get("samplePeriodSecs", 0)),
        )


@dataclass(frozen=True)
class NetworkStats:
    epoch: int
    slot_index: int
    slots_in_epoch: int
    absolute_slot: int
    block_height: int | None
    transaction_count: int | None
    samples: tuple[PerformanceSample, ...]  # newest first
    total_supply_lamports: int | None
    circulating_supply_lamports: int | None
    node_version: str | None

    @property
    def epoch_progress(self) -> float:
        return self.slot_index / self.slots_in_epoch if self.slots_in_epoch else 0.0

    @property
    def average_slot_seconds(self) -> float | None:
        slots = sum(s.num_slots for s in self.samples)
        seconds = sum(s.period_secs for s in self.samples)
        return seconds / slots if slots else None

    @property
    def epoch_eta_seconds(self) -> float | None:
        slot_time = self.average_slot_seconds
        if slot_time is None:
            return None
        return (self.slots_in_epoch - self.slot_index) * slot_time

    @property
    def tps(self) -> float | None:
        return self.samples[0].tps if self.samples else None

    @property
    def non_vote_tps(self) -> float | None:
        return self.samples[0].non_vote_tps if self.samples else None

    def to_dict(self) -> dict[str, Any]:
        def sol(lamports: int | None) -> str | None:
            return None if lamports is None else to_jsonable(lamports_to_sol(lamports))

        data = to_jsonable(self)
        data["samples"] = [
            {**to_jsonable(s), "tps": s.tps, "non_vote_tps": s.non_vote_tps} for s in self.samples
        ]
        data.update(
            epoch_progress=self.epoch_progress,
            average_slot_seconds=self.average_slot_seconds,
            epoch_eta_seconds=self.epoch_eta_seconds,
            tps=self.tps,
            non_vote_tps=self.non_vote_tps,
            total_supply_sol=sol(self.total_supply_lamports),
            circulating_supply_sol=sol(self.circulating_supply_lamports),
        )
        return data


# -- tokens -------------------------------------------------------------------


@dataclass(frozen=True)
class TokenHolding:
    token_account: str
    mint: str
    amount: Decimal
    decimals: int
    program: str  # "spl-token" or "spl-token-2022"
    name: str | None = None
    symbol: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)

    @classmethod
    def from_rpc(cls, raw: dict[str, Any]) -> TokenHolding | None:
        """Build from one ``getTokenAccountsByOwner`` (``jsonParsed``) entry."""
        data = (raw.get("account") or {}).get("data") or {}
        info = (data.get("parsed") or {}).get("info") or {}
        token_amount = info.get("tokenAmount") or {}
        if "mint" not in info:
            return None
        decimals = int(token_amount.get("decimals", 0))
        return cls(
            token_account=raw.get("pubkey", ""),
            mint=info["mint"],
            amount=scale_amount(int(token_amount.get("amount", 0)), decimals),
            decimals=decimals,
            program=data.get("program", "spl-token"),
        )


@dataclass(frozen=True)
class TokenHolder:
    token_account: str
    amount: Decimal
    share: float | None  # fraction of total supply

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True)
class TokenInfo:
    mint: str
    program: str | None
    decimals: int
    supply: Decimal
    mint_authority: str | None
    freeze_authority: str | None
    extensions: tuple[str, ...]
    name: str | None = None
    symbol: str | None = None
    metadata_source: str | None = None
    uri: str | None = None
    image: str | None = None
    description: str | None = None
    website: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)

    @classmethod
    def from_account(cls, account: AccountSummary) -> TokenInfo:
        info = (account.parsed or {}).get("info") or {}
        decimals = int(info.get("decimals", 0))
        return cls(
            mint=account.address,
            program=account.program,
            decimals=decimals,
            supply=scale_amount(int(info.get("supply", 0)), decimals),
            mint_authority=info.get("mintAuthority"),
            freeze_authority=info.get("freezeAuthority"),
            extensions=tuple(
                ext.get("extension", "") for ext in info.get("extensions") or [] if ext
            ),
        )
