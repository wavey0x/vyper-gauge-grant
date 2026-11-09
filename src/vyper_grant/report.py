"""A static, self-contained report; no browser RPC or external scripts."""

import json
from pathlib import Path
from .core import ROOT, atomic_json


def render(data, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(data)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    template = (ROOT / "dashboard/template.html").read_text()
    (out / "index.html").write_text(template.replace("/*REPORT_DATA*/{}", payload))
    atomic_json(out / "report.json", data)
