"""Explorer helpers behind the web UI: network stats, blocks and tokens."""

import base64
import struct
from decimal import Decimal

import pytest

from conftest import FakeClient, make_address, make_signature
from solana_explorer import SolanaExplorer
from solana_explorer.cache import TTLCache
from solana_explorer.errors import NotFoundError, RpcError
from solana_explorer.explorer import TOKEN_PROGRAM_IDS, account_kind
from solana_explorer.metadata import METADATA_PROGRAM_ID, metadata_address
from solana_explorer.models import AccountSummary

VOTE = "Vote111111111111111111111111111111111111111"


def block(slot, txs=(), leader="Leader", reward=5000):
    return {
        "blockhash": f"hash{slot}",
        "previousBlockhash": f"hash{slot - 1}",
        "parentSlot": slot - 1,
        "blockTime": 1_700_000_000,
        "blockHeight": slot - 10,
        "rewards": [{"pubkey": leader, "lamports": reward, "rewardType": "Fee"}],
        "transactions": list(txs),
    }


def tx(signature, signer, *, vote=False, err=None, fee=5000):
    keys = [{"pubkey": signer, "signer": True}]
    if vote:
        keys.append({"pubkey": VOTE, "signer": False})
    return {
        "meta": {"err": err, "fee": fee},
        "transaction": {"accountKeys": keys, "signatures": [signature]},
    }


def test_ttl_cache_expires():
    now = [0.0]
    cache = TTLCache(ttl=5, clock=lambda: now[0])
    cache.put("k", 1)
    assert cache.get("k") == 1
    now[0] = 5.1
    assert cache.get("k") is None


def test_network_stats_derived_values():
    client = FakeClient()
    client.epoch = {"epoch": 7, "slotIndex": 100, "slotsInEpoch": 400, "absoluteSlot": 2900}
    client.samples = [
        {
            "slot": 2900,
            "numTransactions": 6000,
            "numNonVoteTransactions": 1800,
            "numSlots": 150,
            "samplePeriodSecs": 60,
        },
        {"slot": 2750, "numTransactions": 3000, "numSlots": 150, "samplePeriodSecs": 60},
    ]
    explorer = SolanaExplorer(client)
    stats = explorer.network_stats()
    assert stats.epoch_progress == 0.25
    assert stats.tps == 100
    assert stats.non_vote_tps == 30
    assert stats.average_slot_seconds == 0.4
    assert stats.epoch_eta_seconds == pytest.approx(120)
    assert stats.total_supply_lamports is None  # supply is only fetched by supply()
    assert stats.node_version == "9.9.9"

    explorer.supply()
    data = explorer.network_stats().to_dict()
    assert data["total_supply_sol"] == "600000000"
    assert data["samples"][1]["non_vote_tps"] is None


def test_latest_blocks_and_transactions():
    client = FakeClient()
    client.tip = 105
    client.produced = [100, 101, 103, 104]  # 102 and 105 were skipped
    for slot in client.produced:
        client.blocks[slot] = block(
            slot,
            [tx(make_signature(slot), "Voter", vote=True), tx(make_signature(slot + 50), "User")],
        )
    explorer = SolanaExplorer(client)

    blocks = explorer.latest_blocks(3)
    assert [b.slot for b in blocks] == [104, 103, 101]
    assert blocks[0].leader == "Leader"
    assert blocks[0].to_dict()["leader_reward_sol"] == "0.000005"

    newest, txs = explorer.latest_transactions(10)
    assert newest.slot == 104
    assert [t.signer for t in txs] == ["User"]  # vote transactions are filtered out


def test_get_block_details_and_errors():
    client = FakeClient()
    client.blocks[50] = block(50, [tx("a", "S1", vote=True), tx("b", "S2", err={"x": 1}, fee=7000)])
    client.blocks[51] = RpcError(-32007, "Slot 51 was skipped")
    explorer = SolanaExplorer(client)

    details = explorer.get_block(50)
    assert details.transaction_count == 2
    assert details.to_dict()["vote_transaction_count"] == 1
    assert details.transactions[1].success is False
    assert explorer.get_block(50) is details  # cached

    with pytest.raises(NotFoundError, match="skipped"):
        explorer.get_block(51)
    with pytest.raises(NotFoundError):
        explorer.get_block(52)  # node returned null


def metaplex_entry(name, symbol):
    def s(value, pad):
        raw = value.encode().ljust(pad, b"\x00")
        return struct.pack("<I", len(raw)) + raw

    data = b"\x04" + b"\x00" * 64 + s(name, 32) + s(symbol, 10) + s("", 200)
    return {"owner": METADATA_PROGRAM_ID, "data": [base64.b64encode(data).decode(), "base64"]}


def holding(mint, raw, decimals, account):
    return {
        "pubkey": account,
        "account": {
            "data": {
                "program": "spl-token",
                "parsed": {
                    "info": {
                        "mint": mint,
                        "tokenAmount": {"amount": str(raw), "decimals": decimals},
                    }
                },
            }
        },
    }


def test_token_metadata_sources_and_cache():
    named, t22, known, unknown = (
        make_address(1),
        make_address(2),
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        make_address(3),
    )
    client = FakeClient()
    client.multiple[f"base64:{metadata_address(named)}"] = metaplex_entry("Named", "NMD")
    client.multiple[f"jsonParsed:{t22}"] = {
        "data": {
            "parsed": {
                "info": {
                    "extensions": [
                        {"extension": "tokenMetadata", "state": {"name": "T22", "symbol": "T2"}}
                    ]
                }
            }
        }
    }
    explorer = SolanaExplorer(client)

    result = explorer.token_metadata([named, t22, known, unknown])
    assert (result[named].symbol, result[named].source) == ("NMD", "metaplex")
    assert (result[t22].symbol, result[t22].source) == ("T2", "token-2022")
    assert (result[known].symbol, result[known].source) == ("USDC", "builtin")
    assert unknown not in result

    calls = len(client.calls)
    explorer.token_metadata([named, unknown])  # both answers (even "nothing") are cached
    assert len(client.calls) == calls


def test_token_holdings_sorted_and_named():
    owner = make_address(9)
    named, empty, unnamed = make_address(4), make_address(5), make_address(6)
    client = FakeClient()
    client.token_accounts[TOKEN_PROGRAM_IDS[0]] = [
        holding(unnamed, 5_000, 2, "acct-unnamed"),
        holding(empty, 0, 6, "acct-empty"),
        holding(named, 1_500_000, 6, "acct-named"),
    ]
    client.multiple[f"base64:{metadata_address(named)}"] = metaplex_entry("Named", "NMD")
    holdings = SolanaExplorer(client).get_token_holdings(owner)
    assert [h.token_account for h in holdings] == ["acct-named", "acct-unnamed", "acct-empty"]
    assert holdings[0].amount == Decimal("1.5")
    assert holdings[0].symbol == "NMD"


def test_top_holders_share():
    client = FakeClient()
    client.largest = [
        {"address": "A", "amount": "250", "decimals": 1},
        {"address": "B", "amount": "100", "decimals": 1},
    ]
    holders = SolanaExplorer(client).get_top_holders(make_address(7), Decimal(100))
    assert [(h.token_account, h.amount, h.share) for h in holders] == [
        ("A", Decimal("25.0"), 0.25),
        ("B", Decimal("10.0"), 0.1),
    ]


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        (None, "empty"),
        ({"owner": "11111111111111111111111111111111"}, "wallet"),
        ({"owner": "BPFLoaderUpgradeab1e11111111111111111111111", "executable": True}, "program"),
        (
            {"owner": "Tok", "data": {"program": "spl-token", "parsed": {"type": "mint"}}},
            "token_mint",
        ),
        (
            {"owner": "Tok", "data": {"program": "spl-token-2022", "parsed": {"type": "account"}}},
            "token_account",
        ),
        ({"owner": "Vote", "data": {"program": "vote", "parsed": {"type": "vote"}}}, "vote"),
        ({"owner": "SomeProgram", "data": ["", "base64"]}, "account"),
    ],
)
def test_account_kind(value, kind):
    assert account_kind(AccountSummary.from_rpc("Addr", value)) == kind
