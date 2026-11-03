import json
from datetime import datetime, timezone
from decimal import Decimal

from solana_explorer.models import AccountSummary, SignatureInfo, TransactionDetails

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def keys(raw):
    return [k["pubkey"] for k in raw["transaction"]["message"]["accountKeys"]]


def test_transaction_summary_fields(raw_transaction, signature):
    tx = TransactionDetails.from_rpc(signature, raw_transaction)
    payer = keys(raw_transaction)[0]

    assert tx.signature == signature
    assert tx.success is True
    assert tx.error is None
    assert tx.slot == 311234567
    assert tx.block_time == datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert tx.fee_lamports == 5000
    assert tx.fee_sol == Decimal("0.000005")
    assert tx.compute_units == 18432
    assert tx.version == "0"
    assert tx.fee_payer == payer
    assert tx.signers == (payer,)


def test_transfers_include_sol_checked_and_inner_plain_token(raw_transaction, signature):
    tx = TransactionDetails.from_rpc(signature, raw_transaction)
    payer, recipient, payer_ata, recipient_ata = keys(raw_transaction)[:4]

    sol, checked, inner = tx.transfers
    assert (sol.asset, sol.source, sol.destination) == ("SOL", payer, recipient)
    assert sol.amount == Decimal("0.5")
    assert sol.raw_amount == 500_000_000

    assert (checked.asset, checked.mint, checked.amount) == ("token", USDC, Decimal("12.5"))
    assert (checked.source, checked.destination, checked.authority) == (
        payer_ata,
        recipient_ata,
        payer,
    )

    # A plain ``transfer`` has no mint in the instruction; it is recovered from token balances.
    assert (inner.mint, inner.decimals, inner.amount) == (USDC, 6, Decimal("1"))


def test_balance_changes(raw_transaction, signature):
    tx = TransactionDetails.from_rpc(signature, raw_transaction)
    payer, recipient, payer_ata, recipient_ata = keys(raw_transaction)[:4]

    assert {(c.account, c.change_lamports) for c in tx.sol_changes} == {
        (payer, -500_005_000),
        (recipient, 500_000_000),
    }

    changes = {c.account: c for c in tx.token_changes}
    assert changes[payer_ata].change == Decimal("-13.5")
    assert changes[payer_ata].post_amount == Decimal("6.5")
    # The recipient token account did not exist before the transaction.
    assert changes[recipient_ata].pre_amount == 0
    assert changes[recipient_ata].change == Decimal("13.5")
    assert changes[recipient_ata].owner == recipient


def test_instructions_in_execution_order(raw_transaction, signature):
    tx = TransactionDetails.from_rpc(signature, raw_transaction)
    summary = [(ix.position, ix.program, ix.type, ix.inner) for ix in tx.instructions]
    router = keys(raw_transaction)[7]
    assert summary == [
        ("1", "Compute Budget Program", None, False),
        ("2", "System Program", "transfer", False),
        ("3", "Token Program", "transferChecked", False),
        ("4", router, None, False),
        ("4.1", "Token Program", "transfer", True),
    ]


def test_failed_transaction(raw_transaction, signature):
    raw_transaction["meta"]["err"] = {"InstructionError": [2, {"Custom": 1}]}
    tx = TransactionDetails.from_rpc(signature, raw_transaction)
    assert tx.success is False
    assert tx.error == {"InstructionError": [2, {"Custom": 1}]}


def test_legacy_transaction_without_optional_fields(signature):
    tx = TransactionDetails.from_rpc(signature, {"slot": 1, "meta": None, "transaction": {}})
    assert tx.version == "legacy"
    assert tx.block_time is None
    assert tx.transfers == ()
    assert tx.fee_payer is None


def test_to_dict_is_json_serializable(raw_transaction, signature):
    data = TransactionDetails.from_rpc(signature, raw_transaction).to_dict()
    encoded = json.loads(json.dumps(data))
    assert encoded["fee_sol"] == "0.000005"
    assert encoded["block_time"] == "2025-01-01T12:00:00+00:00"
    assert encoded["transfers"][0]["amount"] == "0.5"
    assert encoded["signers"] == [encoded["accounts"][0]["pubkey"]]


def test_account_summary_token_account():
    value = {
        "lamports": 2_039_280,
        "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "executable": False,
        "space": 165,
        "data": {
            "program": "spl-token",
            "parsed": {"type": "account", "info": {"mint": USDC}},
            "space": 165,
        },
    }
    account = AccountSummary.from_rpc("Addr", value)
    assert account.exists
    assert account.balance_sol == Decimal("0.00203928")
    assert (account.program, account.account_type) == ("spl-token", "account")
    assert account.to_dict()["balance_sol"] == "0.00203928"


def test_account_summary_missing_and_raw_data():
    assert AccountSummary.from_rpc("Addr", None).exists is False
    raw = AccountSummary.from_rpc("Addr", {"lamports": 1, "data": ["AAAA", "base64"]})
    assert raw.parsed is None and raw.program is None


def test_signature_info():
    info = SignatureInfo.from_rpc(
        {"signature": "s", "slot": 9, "blockTime": None, "err": {"x": 1}, "memo": "hi"}
    )
    assert (info.success, info.memo, info.block_time) == (False, "hi", None)
