"""Serve the dashboard on this machine from the latest finalized mainnet report.

Run from the repository: python -m vyper_grant.server [--port 8787]

One background thread owns the RPC client and rebuilds the report when
finality advances. Requests only read the last good report, so pages load
instantly; a failing rebuild keeps that report and says so on the page.
"""

import argparse
import json
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .chain import report
from .core import load_config, rpc
from .report import page

HOST = "127.0.0.1"  # Loopback only: there is deliberately no option to change it.
CHECK_SECONDS = 60


def describe(error):
    """Provider errors may contain authenticated endpoint URLs."""
    text = str(error)
    if isinstance(error, (ValueError, FileNotFoundError)) and "http" not in text.lower():
        return text
    return type(error).__name__


class State:
    """The report being served and the current refresh failure, if any."""

    def __init__(self, data):
        self.lock = threading.Lock()
        self.data = data
        self.error = None
        self.failing_since = None

    def snapshot(self):
        with self.lock:
            return self.data, self.error, self.failing_since


def refresh_once(state, client, c):
    """Rebuild the report if finality has advanced past the one being served."""
    data, _, _ = state.snapshot()
    try:
        finalized = client.eth.get_block("finalized")["number"]
        if finalized <= int(data["block"]):
            return False
        fresh = report(client, c)
    except Exception as error:
        message = describe(error)
        with state.lock:
            state.error = message
            if state.failing_since is None:
                state.failing_since = int(time.time())
        print(f"Refresh failed: {message}", file=sys.stderr, flush=True)
        return False
    with state.lock:
        state.data, state.error, state.failing_since = fresh, None, None
    print(f"Updated to finalized block {fresh['block']}", flush=True)
    return True


def refresh_forever(state, client, c):
    while True:
        time.sleep(CHECK_SECONDS)
        refresh_once(state, client, c)


def current_page(state):
    data, error, since = state.snapshot()
    if error:
        at = datetime.fromtimestamp(since, timezone.utc).strftime("%d %b %H:%M UTC")
        notice = (
            f"Mainnet refresh failing since {at} ({error}); "
            f"showing finalized block {data['block']}."
        )
        data = {**data, "warnings": [notice, *data.get("warnings", [])]}
    return page(data)


def handler(state):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                body, kind = current_page(state).encode(), "text/html; charset=utf-8"
            elif path == "/report.json":
                body = (json.dumps(state.snapshot()[0], indent=2) + "\n").encode()
                kind = "application/json"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def main():
    p = argparse.ArgumentParser(description="Serve the Vyper funding dashboard on this machine")
    p.add_argument("--config", default="config/live.json")
    p.add_argument("--port", type=int, default=8787)
    args = p.parse_args()
    try:
        c, client = load_config(args.config), rpc()
        print("Reading finalized mainnet state…", flush=True)
        state = State(report(client, c))
    except Exception as error:
        print("Error: " + describe(error), file=sys.stderr)
        raise SystemExit(1)
    try:
        server = ThreadingHTTPServer((HOST, args.port), handler(state))
    except OSError as error:
        print(f"Error: cannot listen on {HOST}:{args.port} ({error.strerror})", file=sys.stderr)
        raise SystemExit(1)
    threading.Thread(target=refresh_forever, args=(state, client, c), daemon=True).start()
    print(f"Serving http://{HOST}:{args.port} (finalized block {state.data['block']})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
