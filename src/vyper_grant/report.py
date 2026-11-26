"""A static, self-contained report; no browser RPC or external scripts."""

import json
import os
from pathlib import Path
from .chain import report_at_head
from .core import ROOT, atomic_bytes, atomic_json
from .history import save_checkpoint, scan_history
from .share import image


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
    # World-readable: on the server a web server under another user serves these files.
    atomic_json(out / "report.json", data, mode=0o644)
    # The link-preview image, rewritten only when its figures or day change.
    preview, png = out / "og.png", image(data)
    if not preview.exists() or preview.read_bytes() != png:
        atomic_bytes(preview, png, mode=0o644)


def collect(client, config, out):
    """Publish the report before advancing the private event checkpoint."""
    import fcntl

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    checkpoint = out / ".events.json"
    directory = os.open(out, os.O_RDONLY)
    try:
        # Lock the directory: both the report and checkpoint are atomically replaced.
        fcntl.flock(directory, fcntl.LOCK_EX)
        head = client.eth.get_block("latest")
        history = scan_history(client, config, head, checkpoint)
        render(report_at_head(client, config, head, history), out)
        save_checkpoint(checkpoint, history)
    finally:
        os.close(directory)
