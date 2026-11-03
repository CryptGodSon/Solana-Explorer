import pytest

from conftest import FakeClient, make_address, make_signature
from solana_explorer import (
    InvalidInputError,
    SolanaExplorer,
    TransactionNotFoundError,
)


def test_get_transaction_is_cached(fake_client, signature):
    explorer = SolanaExplorer(fake_client)
    first = explorer.get_transaction(signature)
    second = explorer.get_transaction(f"  {signature}  ")
    assert first is second
    assert fake_client.calls == [("getTransaction", signature)]


def test_cache_can_be_disabled_and_cleared(fake_client, signature):
    explorer = SolanaExplorer(fake_client, cache_size=0)
    explorer.get_transaction(signature)
    explorer.get_transaction(signature)
    assert len(fake_client.calls) == 2

    cached = SolanaExplorer(fake_client)
    cached.get_transaction(signature)
    cached.clear_cache()
    cached.get_transaction(signature)
    assert len(fake_client.calls) == 4


def test_missing_transaction_raises():
    explorer = SolanaExplorer(FakeClient())
    with pytest.raises(TransactionNotFoundError):
        explorer.get_transaction(make_signature(1))


def test_invalid_signature_never_hits_rpc():
    client = FakeClient()
    with pytest.raises(InvalidInputError):
        SolanaExplorer(client).get_transaction("INSERT_TRANSACTION_ID_HERE")
    assert client.calls == []


def test_batch_lookup_collects_errors_and_dedupes(fake_client, signature):
    missing = make_signature(2)
    results = SolanaExplorer(fake_client).get_transactions(
        f"{signature}, bogus\n{missing} {signature}"
    )
    assert [r.signature for r in results] == [signature, "bogus", missing]
    assert [r.ok for r in results] == [True, False, False]
    assert isinstance(results[1].error, InvalidInputError)
    assert isinstance(results[2].error, TransactionNotFoundError)


def test_get_account_and_signatures():
    address = make_address(3)
    client = FakeClient(
        accounts={address: {"lamports": 5, "owner": "11111111111111111111111111111111"}},
        signatures=[{"signature": make_signature(i), "slot": i, "err": None} for i in range(5)],
    )
    explorer = SolanaExplorer(client)
    assert explorer.get_account(address).lamports == 5
    history = explorer.get_signatures(address, limit=3)
    assert [s.slot for s in history] == [0, 1, 2]


def test_signature_limit_is_clamped():
    client = FakeClient()
    SolanaExplorer(client).get_signatures(make_address(4), limit=10_000)
    assert client.calls[0][1][1] == 1000


def test_client_and_endpoint_are_mutually_exclusive(fake_client):
    with pytest.raises(TypeError):
        SolanaExplorer(fake_client, endpoint="devnet")
