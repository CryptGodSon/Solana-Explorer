from __future__ import annotations

from typing import Any

import pytest
import requests

from solana_explorer.errors import InvalidInputError, RpcError, RpcTransportError
from solana_explorer.rpc import SolanaRpcClient, cluster_name, resolve_endpoint


class FakeResponse:
    def __init__(self, status: int = 200, body: Any = None, headers: dict | None = None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = "" if body is None else str(body)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    def __init__(self, *responses: FakeResponse | Exception):
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.headers: dict[str, str] = {}

    def post(self, url: str, json: dict, timeout: float) -> FakeResponse:
        self.requests.append({"url": url, "json": json, "timeout": timeout})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        pass


def make_client(*responses, **kwargs) -> tuple[SolanaRpcClient, FakeSession]:
    session = FakeSession(*responses)
    client = SolanaRpcClient("devnet", session=session, backoff=0, **kwargs)
    return client, session


def ok(result: Any) -> FakeResponse:
    return FakeResponse(200, {"jsonrpc": "2.0", "id": 1, "result": result})


def test_resolve_endpoint(monkeypatch):
    monkeypatch.delenv("SOLANA_RPC_URL", raising=False)
    assert resolve_endpoint(None) == "https://api.mainnet-beta.solana.com"
    assert resolve_endpoint("mainnet") == "https://api.mainnet-beta.solana.com"
    assert resolve_endpoint("DevNet") == "https://api.devnet.solana.com"
    assert resolve_endpoint("https://rpc.example.com") == "https://rpc.example.com"
    monkeypatch.setenv("SOLANA_RPC_URL", "https://env.example.com")
    assert resolve_endpoint(None) == "https://env.example.com"
    with pytest.raises(InvalidInputError):
        resolve_endpoint("moonnet")


def test_cluster_name():
    assert cluster_name("https://api.devnet.solana.com/") == "devnet"
    assert cluster_name("https://rpc.example.com") is None


def test_call_sends_json_rpc_payload():
    client, session = make_client(ok({"value": None}), timeout=3)
    assert client.get_account_info("Addr") is None
    request = session.requests[0]
    assert request["url"] == "https://api.devnet.solana.com"
    assert request["timeout"] == 3
    assert request["json"]["jsonrpc"] == "2.0"
    assert request["json"]["method"] == "getAccountInfo"
    assert request["json"]["params"] == [
        "Addr",
        {"encoding": "jsonParsed", "commitment": "confirmed"},
    ]


def test_get_transaction_requests_versioned_parsed_tx():
    client, session = make_client(ok(None))
    assert client.get_transaction("Sig") is None
    params = session.requests[0]["json"]["params"]
    assert params[0] == "Sig"
    assert params[1]["encoding"] == "jsonParsed"
    assert params[1]["maxSupportedTransactionVersion"] == 0


def test_get_signatures_for_address_options():
    client, session = make_client(ok([{"signature": "a"}]))
    assert client.get_signatures_for_address("Addr", limit=5, before="B") == [{"signature": "a"}]
    assert session.requests[0]["json"]["params"][1] == {
        "limit": 5,
        "commitment": "confirmed",
        "before": "B",
    }


def test_json_rpc_error_is_raised():
    error = {"code": -32602, "message": "Invalid param: WrongSize"}
    client, _ = make_client(FakeResponse(200, {"jsonrpc": "2.0", "id": 1, "error": error}))
    with pytest.raises(RpcError) as info:
        client.call("getTransaction", ["x"])
    assert info.value.code == -32602
    assert "WrongSize" in str(info.value)


def test_retries_on_rate_limit_then_succeeds():
    client, session = make_client(FakeResponse(429), FakeResponse(503), ok(42))
    assert client.call("getSlot") == 42
    assert len(session.requests) == 3


def test_gives_up_after_max_retries():
    client, session = make_client(FakeResponse(429), FakeResponse(429), max_retries=1)
    with pytest.raises(RpcTransportError, match="Rate limited"):
        client.call("getSlot")
    assert len(session.requests) == 2


def test_network_errors_are_retried_and_wrapped():
    boom = requests.ConnectionError("connection refused")
    client, session = make_client(boom, boom, max_retries=1)
    with pytest.raises(RpcTransportError, match="Could not reach"):
        client.call("getSlot")
    assert len(session.requests) == 2


def test_non_json_response():
    client, _ = make_client(FakeResponse(200, None))
    with pytest.raises(RpcTransportError, match="Unexpected response"):
        client.call("getSlot")


def test_http_error_without_rpc_body():
    client, _ = make_client(FakeResponse(403, None), max_retries=0)
    with pytest.raises(RpcTransportError, match="HTTP 403"):
        client.call("getSlot")
