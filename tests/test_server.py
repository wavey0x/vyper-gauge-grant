import json
import threading
import urllib.error
import urllib.request

import pytest

from vyper_grant import server

DATA = {
    "block": "100",
    "as_of": "1795475159",
    "warnings": ["Sales are paused; public checkpoint remains available"],
    "weeks": [],
    "proceeds_balance": str(10**30 + 1),
}


class Client:
    """Only the finalized block number is read outside report()."""

    def __init__(self, finalized):
        self.eth = self
        self.finalized = finalized

    def get_block(self, tag):
        assert tag == "finalized"
        return {"number": self.finalized}


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


def test_rebuilds_only_when_finality_advances(monkeypatch):
    built = []

    def build(client, c):
        built.append(client.finalized)
        return {**DATA, "block": str(client.finalized)}

    monkeypatch.setattr(server, "report", build)
    state = server.State(dict(DATA))
    assert server.refresh_once(state, Client(100), {}) is False
    assert built == []
    assert server.refresh_once(state, Client(101), {}) is True
    assert built == [101]
    assert state.snapshot()[0]["block"] == "101"


def test_failed_rebuild_keeps_the_last_report_and_says_so(monkeypatch):
    def broken(client, c):
        raise ConnectionError("https://rpc.example/secret-key timed out")

    monkeypatch.setattr(server, "report", broken)
    state = server.State(dict(DATA))
    assert server.refresh_once(state, Client(101), {}) is False
    data, error, _ = state.snapshot()
    assert data == DATA
    assert error == "ConnectionError"
    html = server.current_page(state)
    assert "Mainnet refresh failing since" in html
    assert "(ConnectionError); showing finalized block 100." in html
    assert "secret-key" not in html

    monkeypatch.setattr(server, "report", lambda client, c: {**DATA, "block": "102"})
    assert server.refresh_once(state, Client(102), {}) is True
    assert "Mainnet refresh failing" not in server.current_page(state)
