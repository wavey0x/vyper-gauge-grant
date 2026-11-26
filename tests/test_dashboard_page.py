"""The dashboard page, its report file and the hosting config that points at them."""

import json
import re
import stat
from urllib.parse import urlparse

import pytest
from PIL import Image

from vyper_grant.chain import report
from vyper_grant.core import ROOT
from vyper_grant.report import page, render
from vyper_grant.share import CAP, figures


def test_report_reads_the_latest_block_by_default():
    asked = []

    class Client:
        def __init__(self):
            self.eth = self

        def get_block(self, tag):
            asked.append(tag)
            return {"number": 5}

    with pytest.raises(ValueError, match="deployment_block"):
        report(Client(), {"deployment_block": 10})
    assert asked == ["latest"]


def test_report_file_is_readable_by_the_web_server(tmp_path):
    render({"weeks": []}, tmp_path)
    assert stat.S_IMODE((tmp_path / "report.json").stat().st_mode) == 0o644


def test_page_without_an_embedded_report_loads_one():
    template = (ROOT / "dashboard/template.html").read_text()
    assert template.count("/*REPORT_DATA*/{}") == 1
    assert "fetch('report.json'" in template
    assert '"as_of": "1"' in page({"as_of": "1"})


def test_vercel_serves_the_template_and_forwards_the_report():
    config = json.loads((ROOT / "vercel.json").read_text())
    rewrites = {r["source"]: r["destination"] for r in config["rewrites"]}
    assert (ROOT / config["outputDirectory"] / rewrites["/"].lstrip("/")).is_file()
    assert rewrites["/report.json"].startswith("https://")
    # Served through the rewrite, the page would otherwise name itself template.html, and
    # Telegram then previews it as a file rather than a page.
    root = {h["key"]: h["value"] for rule in config["headers"] if rule["source"] == "/" for h in rule["headers"]}
    assert root["Content-Disposition"] == "inline"


def test_preview_figures_round_like_the_page():
    data = {"funding_assets": "1", "spent": str(10_000 * 10**6), "totals": {"yearn": "873620000"}}
    assert figures(data) == [("874", "Proceeds"), ("10,000", "Budget spent"), ("90,000", "Budget remaining")]
    assert figures(dict(data, spent=str(120_000 * 10**6)))[2] == ("0", "Budget remaining")
    assert [value for value, _ in figures({"weeks": []})] == ["—"] * 3
    assert f"const TARGET = {CAP // 10**6:_};" in (ROOT / "dashboard/template.html").read_text()


def test_preview_image_is_rewritten_only_when_it_changes(tmp_path):
    data = {"as_of": "1794250000", "funding_assets": "1", "spent": str(10_000 * 10**6)}
    render(data, tmp_path)
    preview = tmp_path / "og.png"
    assert stat.S_IMODE(preview.stat().st_mode) == 0o644
    assert Image.open(preview).size == (1200, 630)
    first = preview.stat().st_ino
    render(dict(data, weeks=[]), tmp_path)
    assert preview.stat().st_ino == first
    render(dict(data, spent=str(20_000 * 10**6)), tmp_path)
    assert preview.stat().st_ino != first


def test_page_names_a_preview_image_the_route_serves():
    template = (ROOT / "dashboard/template.html").read_text()
    url = re.search(r'property="og:image" content="([^"]+)"', template)[1]
    assert url.startswith("https://api.wavey.info/")
    assert f"handle {urlparse(url).path} {{" in (ROOT / "deploy/caddy/vyper-grant.caddy").read_text()
