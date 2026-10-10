from decimal import Decimal

from solana_explorer.formatting import (
    explorer_url,
    format_amount,
    format_sol,
    format_transaction,
    label,
)
from solana_explorer.models import TransactionDetails

MAINNET = "https://api.mainnet-beta.solana.com"


def test_amount_formatting():
    assert format_amount(Decimal("1234.5000")) == "1,234.5"
    assert format_amount(Decimal("100")) == "100"
    assert format_amount(Decimal("-0.25"), signed=True) == "-0.25"
    assert format_sol(5000) == "0.000005 SOL"
    assert format_sol(1_500_000_000, signed=True) == "+1.5 SOL"


def test_explorer_urls():
    assert explorer_url("tx", "abc", MAINNET) == "https://explorer.solana.com/tx/abc"
    assert explorer_url("address", "abc", "https://api.devnet.solana.com") == (
        "https://explorer.solana.com/address/abc?cluster=devnet"
    )
    assert explorer_url("tx", "abc", "http://localhost:8899") == (
        "https://explorer.solana.com/tx/abc?cluster=custom&customUrl=http%3A%2F%2Flocalhost%3A8899"
    )


def test_label_names_known_programs():
    assert label("11111111111111111111111111111111").endswith("(System Program)")
    assert label("SomethingElse") == "SomethingElse"
    assert label(None) == "unknown"


def test_transaction_report(raw_transaction, signature):
    tx = TransactionDetails.from_rpc(signature, raw_transaction)
    report = format_transaction(tx, MAINNET)
    assert report.startswith(f"Transaction {signature}")
    assert "Status:          Success" in report
    assert "Fee:             0.000005 SOL" in report
    assert "Transfers (3)" in report
    assert "0.5 SOL" in report
    assert "12.5 USDC" in report
    assert "#4.1" in report
    assert "Log messages" not in report
    assert "Log messages (12)" in format_transaction(tx, MAINNET, show_logs=True)
    report.encode("ascii")  # safe for any console encoding


def test_failed_transaction_report(raw_transaction, signature):
    raw_transaction["meta"]["err"] = {"InstructionError": [2, {"Custom": 1}]}
    report = format_transaction(TransactionDetails.from_rpc(signature, raw_transaction), MAINNET)
    assert 'FAILED - {"InstructionError":[2,{"Custom":1}]}' in report
