import http.client
import json
import threading

import pytest

from conftest import make_address, make_signature
from solana_explorer import SolanaExplorer
from solana_explorer.web.server import ExplorerWebApp, create_server

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


@pytest.fixture
def web(fake_client):
    created = []

    def factory(endpoint):
        created.append(endpoint)
        return SolanaExplorer(fake_client)

    app = ExplorerWebApp("mainnet-beta", explorer_factory=factory)
    app.created = created
    return app


def get(app, path, **query):
    return app.handle(path, {k: [v] for k, v in query.items()})


def test_config_lists_clusters_and_labels(web):
    status, data = get(web, "/api/config")
    assert status == 200
    assert data["default_cluster"] == "mainnet-beta"
    assert data["clusters"] == ["mainnet-beta", "devnet", "testnet"]
    assert data["labels"][USDC] == "USDC"


def test_custom_rpc_url_becomes_custom_cluster():
    app = ExplorerWebApp("https://rpc.example.com", explorer_factory=lambda url: url)
    assert app.default_cluster == "custom"
    assert app.endpoints["custom"] == "https://rpc.example.com"


def test_one_explorer_per_cluster_and_unknown_cluster(web):
    get(web, "/api/config")
    get(web, "/api/tx/" + make_signature(1))
    get(web, "/api/tx/" + make_signature(1), cluster="devnet")
    assert web.created == ["https://api.mainnet-beta.solana.com", "https://api.devnet.solana.com"]
    status, data = get(web, "/api/network", cluster="https://evil.example")
    assert status == 400
    assert "Unknown cluster" in data["error"]


def test_transaction_endpoint_includes_token_labels(web, signature):
    status, data = get(web, f"/api/tx/{signature}")
    assert status == 200
    assert data["signature"] == signature
    assert data["tokens"][USDC]["symbol"] == "USDC"
    assert data["instructions"][3]["accounts"]  # unparsed instruction keeps its accounts


def test_error_statuses(web):
    assert get(web, "/api/tx/not-a-signature")[0] == 400
    assert get(web, "/api/tx/" + make_signature(5))[0] == 404
    assert get(web, "/api/block/77")[0] == 404
    assert get(web, "/api/blocks/latest", limit="abc")[0] == 400
    assert get(web, "/api/unknown")[0] == 404


@pytest.mark.parametrize(
    ("term", "expected"),
    [("12345", "block"), (make_signature(1), "tx"), (make_address(1), "account")],
)
def test_search(web, term, expected):
    status, data = get(web, "/api/search", q=f"  {term} ")
    assert status == 200
    assert data == {"type": expected, "value": term}


def test_search_rejects_garbage(web):
    assert get(web, "/api/search", q="hello")[0] == 400


def test_account_endpoint_token_mint(fake_client, web):
    mint = make_address(2)
    fake_client.accounts[mint] = {
        "lamports": 1,
        "owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "data": {
            "program": "spl-token",
            "parsed": {
                "type": "mint",
                "info": {"decimals": 2, "supply": "12345", "mintAuthority": None},
            },
        },
    }
    status, data = get(web, f"/api/account/{mint}")
    assert status == 200
    assert data["kind"] == "token_mint"
    assert data["token"]["supply"] == "123.45"


def test_supply_is_sent_as_strings(web):
    status, data = get(web, "/api/supply")
    assert status == 200
    assert data["total_sol"] == "600000000"


@pytest.fixture
def server(web):
    srv = create_server(web, "127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def request(server, path, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    headers = {"Host": host} if host else {}
    conn.request("GET", path, headers=headers)
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response, body


def test_server_serves_app_api_and_static(server):
    response, body = request(server, "/tx/whatever")
    assert response.status == 200
    assert b'<main id="app"' in body
    assert "script-src 'self'" in response.getheader("Content-Security-Policy")

    response, body = request(server, "/api/config")
    assert response.getheader("Content-Type").startswith("application/json")
    assert json.loads(body)["default_cluster"] == "mainnet-beta"

    response, body = request(server, "/static/app.js")
    assert response.status == 200
    assert response.getheader("Content-Type").startswith("text/javascript")


def test_server_never_serves_files_outside_static(server):
    _, body = request(server, "/static/../server.py")
    assert b"import" not in body
    assert b'<main id="app"' in body


def test_server_rejects_foreign_host_header(server):
    response, _ = request(server, "/api/config", host="attacker.example:80")
    assert response.status == 403
