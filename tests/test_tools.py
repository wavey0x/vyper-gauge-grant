import json
import pytest
from vyper_grant.report import render
from vyper_grant.core import load_config


def test_report_escapes_script_breakout_and_handles_empty(tmp_path):
    data = {
        "warnings": ["</script><script>alert(1)</script>"],
        "weeks": [],
        "allocations": [],
    }
    render(data, tmp_path)
    text = (tmp_path / "index.html").read_text()
    assert "</script><script>alert(1)" not in text
    assert "\\u003c/script\\u003e" in text
    assert json.loads((tmp_path / "report.json").read_text()) == data


def test_production_addresses_cannot_be_redirected(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"addresses": {"gauge": "0x" + "12" * 20}}))
    with pytest.raises(ValueError, match="cannot be overridden"):
        load_config(p)
