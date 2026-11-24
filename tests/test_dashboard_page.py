"""The dashboard page, its report file and the hosting config that points at them."""

import json
import stat

import pytest

from vyper_grant.chain import report
from vyper_grant.core import ROOT
from vyper_grant.report import page, render


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
