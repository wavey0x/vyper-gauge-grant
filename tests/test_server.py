import json
import threading
import urllib.error
import urllib.request

import pytest
from web3 import Web3

from vyper_grant import server

DATA = {
    "block": "100",
    "block_hash": Web3.to_hex((100).to_bytes(32, "big")),
    "as_of": "1795475159",
    "warnings": ["Sales are paused; public checkpoint remains available"],
    "weeks": [],
    "proceeds_balance": str(10**30 + 1),
}


class Client:
    to_hex = staticmethod(Web3.to_hex)

    def __init__(self, number, hash_value=None):
        self.eth = self
        self.head = {"number": number, "hash": (hash_value or number).to_bytes(32, "big")}

    def get_block(self, tag):
        assert tag == "latest"
        return self.head


@pytest.fixture
def served():
    state = server.State(dict(DATA))
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.handler(state))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def get(url):
    with urllib.request.urlopen(url) as response:
        return response.headers, response.read().decode()


def test_serves_the_page_and_its_data(served):
    headers, html = get(served + "/")
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert headers["Cache-Control"] == "no-store"
    assert '"proceeds_balance": "1000000000000000000000000000001"' in html
    assert "Sales are paused" in html

    headers, body = get(served + "/report.json")
    assert headers["Cache-Control"] == "no-store"
    assert json.loads(body) == DATA


def test_other_paths_are_not_found(served):
    with pytest.raises(urllib.error.HTTPError) as missing:
        get(served + "/config/live.json")
    assert missing.value.code == 404


@pytest.mark.parametrize(("number", "hash_value"), [(101, 101), (100, 200), (99, 99)])
def test_publishes_new_and_reorganized_heads(monkeypatch, number, hash_value):
    def build(client, c, *, head):
        assert head is client.head
        return {**DATA, "block": str(head["number"]), "block_hash": Web3.to_hex(head["hash"])}

    monkeypatch.setattr(server, "report", build)
    state = server.State(dict(DATA))
    assert server.refresh_once(state, Client(100), {}) is False
    assert server.refresh_once(state, Client(number, hash_value), {}) is True
    assert state.snapshot()[0]["block_hash"] == Web3.to_hex(hash_value.to_bytes(32, "big"))


def test_failed_rebuild_keeps_the_last_report_and_says_so(monkeypatch):
    def broken(client, c, *, head):
        raise ConnectionError("https://rpc.example/secret-key timed out")

    monkeypatch.setattr(server, "report", broken)
    state = server.State(dict(DATA))
    assert server.refresh_once(state, Client(101), {}) is False
    data, error, _ = state.snapshot()
    assert data == DATA
    assert error == "ConnectionError"
    html = server.page(server.current_data(state))
    assert "Mainnet refresh failing since" in html
    assert "(ConnectionError); showing block 100." in html
    assert "secret-key" not in html

    assert server.refresh_once(state, Client(100), {}) is False
    assert "Mainnet refresh failing" not in server.page(server.current_data(state))
