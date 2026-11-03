from pathlib import Path
import json
from .core import ROOT


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
    (out / "report.json").write_text(json.dumps(data, indent=2) + "\n")
