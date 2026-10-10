import json

import pytest

from conftest import FakeClient, make_address, make_signature
from solana_explorer import SolanaExplorer, __version__
from solana_explorer.cli import main


def run(argv, client):
    seen = {}

    def factory(endpoint, timeout):
        seen.update(endpoint=endpoint, timeout=timeout)
        return SolanaExplorer(client)

    return main(argv, explorer_factory=factory), seen


def test_tx_text_output(fake_client, signature, capsys):
    code, seen = run(["tx", signature, "--cluster", "devnet", "--timeout", "5"], fake_client)
    out = capsys.readouterr().out
    assert code == 0
    assert seen == {"endpoint": "devnet", "timeout": 5.0}
    assert f"Transaction {signature}" in out
    assert "Transfers (3)" in out


def test_tx_json_single_and_batch(fake_client, signature, capsys):
    code, _ = run(["tx", signature, "--json"], fake_client)
    single = json.loads(capsys.readouterr().out)
    assert code == 0
    assert single["signature"] == signature

    missing = make_signature(9)
    code, _ = run(["tx", signature, missing, "--json"], fake_client)
    batch = json.loads(capsys.readouterr().out)
    assert code == 1
    assert batch[0]["fee_sol"] == "0.000005"
    assert batch[1]["signature"] == missing
    assert "not found" in batch[1]["error"]


def test_tx_reads_signatures_from_file(fake_client, signature, tmp_path, capsys):
    path = tmp_path / "sigs.txt"
    path.write_text(f"{signature}\n{signature},\n", encoding="utf-8")
    code, _ = run(["tx", "--file", str(path)], fake_client)
    assert code == 0
    assert capsys.readouterr().out.count("Transaction ") == 1


def test_tx_without_signatures_is_usage_error(fake_client, capsys):
    with pytest.raises(SystemExit) as info:
        run(["tx"], fake_client)
    assert info.value.code == 2
    assert "at least one signature" in capsys.readouterr().err


def test_invalid_signature_reported_on_stderr(fake_client, capsys):
    code, _ = run(["tx", "INSERT_TRANSACTION_ID_HERE"], fake_client)
    captured = capsys.readouterr()
    assert code == 1
    assert "not a valid transaction signature" in captured.err


def test_account_and_history(capsys):
    address = make_address(1)
    client = FakeClient(
        accounts={
            address: {"lamports": 1_000_000_000, "owner": "11111111111111111111111111111111"}
        },
        signatures=[{"signature": make_signature(1), "slot": 7, "blockTime": 0, "err": None}],
    )
    assert run(["account", address], client)[0] == 0
    out = capsys.readouterr().out
    assert "Balance:         1 SOL" in out
    assert "(System Program)" in out

    assert run(["history", address, "-n", "1", "--json"], client)[0] == 0
    history = json.loads(capsys.readouterr().out)
    assert history[0]["slot"] == 7


def test_invalid_address_is_usage_error(capsys):
    with pytest.raises(SystemExit) as info:
        run(["account", "nope"], FakeClient())
    assert info.value.code == 2


def test_version(capsys):
    with pytest.raises(SystemExit):
        main(["--version"])
    assert __version__ in capsys.readouterr().out
