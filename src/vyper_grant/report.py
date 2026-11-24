"""A static, self-contained report; no browser RPC or external scripts."""

import json
from pathlib import Path
from .core import ROOT, atomic_json


def page(data):
    """Return the dashboard HTML with the report embedded as inert script data."""
    payload = (
        json.dumps(data)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    template = (ROOT / "dashboard/template.html").read_text()
    return template.replace("/*REPORT_DATA*/{}", payload)


def render(data, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(page(data))
    # World-readable: on the server a web server under another user serves this file.
    atomic_json(out / "report.json", data, mode=0o644)
