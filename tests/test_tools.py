import json
import pytest
from vyper_grant.report import render
from vyper_grant.core import ROOT, load_config, units


def test_report_escapes_script_breakout_and_preserves_precision(tmp_path):
    data = {
        "warnings": ["</script><script>alert(1)</script>"],
        "weeks": [],
        "proceeds_balance": str(10**30 + 1),
    }
    render(data, tmp_path)
    text = (tmp_path / "index.html").read_text()
    assert "</script><script>alert(1)" not in text
    assert "\\u003c/script\\u003e" in text
    assert json.loads((tmp_path / "report.json").read_text()) == data


def test_legacy_configuration_cannot_redirect_dependencies(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"addresses": {"gauge": "0x" + "12" * 20}}))
    with pytest.raises(ValueError, match="obsolete"):
        load_config(p)


def test_config_and_decimal_validation(tmp_path):
    c = json.loads((ROOT / "config/example.json").read_text())
    p = tmp_path / "config.json"
    p.write_text(json.dumps(c))
    assert "start" not in load_config(p) and "weeks" not in load_config(p)
    c["slippage_bps"] = 10000
    p.write_text(json.dumps(c))
    with pytest.raises(ValueError, match="Slippage"):
        load_config(p)
    c.update(slippage_bps=50, max_crv="99")
    p.write_text(json.dumps(c))
    with pytest.raises(ValueError, match="Maximum CRV"):
        load_config(p)
    assert units("0.000001", 6) == 1
    assert units("100000.000000000000000001") == 100000 * 10**18 + 1
    for n in ["-1", "NaN", "Infinity", "0.0000001"]:
        with pytest.raises(ValueError):
            units(n, 6)


def test_amount_conversion_does_not_round_at_decimal_context_precision():
    assert units("10000000000.000000000000000001") == 10**28 + 1
    with pytest.raises(ValueError):
        units("1." + "0" * 100 + "1")


def test_generated_artifacts_match_contract_sources():
    from vyper import compile_code
    from vyper_grant.core import CONTRACTS, artifact

    for name, source in CONTRACTS.items():
        expected = compile_code(
            (ROOT / "contracts" / (source + ".vy")).read_text(),
            output_formats=["abi", "bytecode", "bytecode_runtime"],
        )
        assert artifact(name) == expected
