from fractions import Fraction
import pytest
from vyper_grant.core import credit, value_for_week, usdc


def test_ratio_converts_cvx_and_deduplicates():
    ratio = credit(1000, 500, 400, 25, 100)
    assert ratio == Fraction(4, 5)
    v = value_for_week(0, 10000 * 10**18, 10000 * 10**18, ratio, 25_000_000)
    assert (
        v["credited_crv"] == 8000 * 10**18 and v["received_value_usdc"] == 2000 * 10**6
    )


def test_partial_mint_credits_only_program_interval():
    v = value_for_week(
        1000 * 10**18, 2000 * 10**18, 1500 * 10**18, Fraction(1, 2), 25_000_000
    )
    assert v["received_credited_crv"] == 250 * 10**18
    assert v["received_value_usdc"] == 62_500_000
    assert (
        value_for_week(1000, 2000, 900, Fraction(1), 25_000_000)["received_value_usdc"]
        == 0
    )


def test_weights_zero_all_blacklisted_and_raw_unit_errors():
    assert credit(0, 0, 0, 0, 0) == 0
    assert credit(1000, 0, 1000, 100, 100) == 0
    with pytest.raises(ValueError):
        credit(1000, 1000, 1000, 0, 100)
    with pytest.raises(ValueError):
        credit(1000, 0, 100, 101, 100)
    with pytest.raises(ValueError):
        credit(1000, 0, 100, 0, 0)


def test_large_integer_rounding_is_exact():
    n = 10**35 + 1
    r = Fraction(1, 3)
    result = value_for_week(0, n, n, r, 27_123_456)
    assert result["received_value_usdc"] == n * 27_123_456 // (3 * 10**20)


def test_usdc_rejects_fractional_base_unit():
    assert usdc("0.000001") == 1
    with pytest.raises(ValueError):
        usdc("0.0000001")
