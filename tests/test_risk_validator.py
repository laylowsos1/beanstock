"""Tests for Beanstock's deterministic risk engine (risk/validator.py).

Pure unit tests. No network access, no broker connection, no order
placement -- every input is a plain in-memory value.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from risk.validator import validate_trade


def base_trade(**overrides):
    trade = {
        "symbol": "TEST",
        "instrument_type": "stock",
        "side": "long",
        "uses_margin": False,
        "cost": 30.0,
        "sector": "Technology",
        "candidate_score": 88,
        "catalyst": "Earnings beat with raised guidance",
        "entry": 10.0,
        "stop": 9.0,
        "target": 12.5,  # reward=2.5, risk=1.0 -> R:R 2.5
        "averaging_down_on_decline": False,
    }
    trade.update(overrides)
    return trade


def call(trade=None, **kwargs):
    params = dict(
        account_equity=300.0,
        available_cash=300.0,
        current_positions=0,
        trades_this_week=0,
        proposed_trade=trade or base_trade(),
        current_company_exposure_pct=0.0,
        current_sector_exposure_pct=0.0,
        blocked_sectors=[],
        account_mode="PAPER",
    )
    params.update(kwargs)
    return validate_trade(**params)


def failing_rule_numbers(result):
    return {c["number"] for c in result["checks"] if c["blocking"] and not c["passed"]}


# ---------------------------------------------------------------------------
# Required scenarios
# ---------------------------------------------------------------------------

def test_200_position_in_300_account_rejected():
    result = call(trade=base_trade(cost=200.0))
    assert result["approved"] is False
    assert result["calculated"]["position_percentage"] == 66.6667 or round(
        result["calculated"]["position_percentage"], 2
    ) == 66.67
    # hard cap, company exposure, and sector exposure should all fail
    failing = failing_rule_numbers(result)
    assert 9 in failing
    assert 10 in failing
    assert 11 in failing


def test_30_position_score_88_rr_2_5_valid_catalyst_approved():
    result = call(trade=base_trade(cost=30.0, candidate_score=88))
    assert result["approved"] is True
    assert result["rejection_reasons"] == []
    assert result["calculated"]["position_percentage"] == 10.0
    assert result["calculated"]["reward_risk"] == 2.5


def test_seventh_position_rejected():
    result = call(trade=base_trade(cost=30.0), current_positions=6)
    assert result["approved"] is False
    assert 7 in failing_rule_numbers(result)


def test_fourth_trade_this_week_rejected():
    result = call(trade=base_trade(cost=30.0), trades_this_week=3)
    assert result["approved"] is False
    assert 8 in failing_rule_numbers(result)


def test_score_70_rejected():
    result = call(trade=base_trade(candidate_score=70))
    assert result["approved"] is False
    assert 12 in failing_rule_numbers(result)


def test_35_percent_sector_exposure_rejected():
    # current sector exposure 25% + this 10% position = 35%
    result = call(trade=base_trade(cost=30.0), current_sector_exposure_pct=25.0)
    assert result["approved"] is False
    assert result["calculated"]["sector_exposure_after_pct"] == 35.0
    assert 11 in failing_rule_numbers(result)


def test_option_rejected():
    result = call(trade=base_trade(instrument_type="option"))
    assert result["approved"] is False
    failing = failing_rule_numbers(result)
    assert 3 in failing
    assert 4 in failing


def test_live_account_rejected():
    result = call(trade=base_trade(cost=30.0), account_mode="LIVE")
    assert result["approved"] is False
    failing = failing_rule_numbers(result)
    assert 1 in failing
    assert 2 in failing


# ---------------------------------------------------------------------------
# One test per remaining hard rule
# ---------------------------------------------------------------------------

def test_rule1_paper_mode_variants_pass():
    for mode in ("PAPER", "SIMULATED", "paper", "simulated"):
        result = call(trade=base_trade(cost=30.0), account_mode=mode)
        assert result["approved"] is True, f"mode={mode} should be approved"


def test_rule3_fractional_share_allowed():
    result = call(trade=base_trade(cost=30.0, instrument_type="fractional_share"))
    assert result["approved"] is True


def test_rule3_crypto_instrument_rejected():
    result = call(trade=base_trade(instrument_type="crypto"))
    assert result["approved"] is False
    assert 3 in failing_rule_numbers(result)


def test_rule5_short_selling_rejected():
    result = call(trade=base_trade(side="short"))
    assert result["approved"] is False
    assert 5 in failing_rule_numbers(result)


def test_rule6_margin_use_rejected():
    result = call(trade=base_trade(uses_margin=True))
    assert result["approved"] is False
    assert 6 in failing_rule_numbers(result)


def test_rule9_small_account_hard_cap_16_percent_rejected():
    # $48 on $300 equity = 16% > 15% hard cap
    result = call(trade=base_trade(cost=48.0))
    assert result["approved"] is False
    assert 9 in failing_rule_numbers(result)


def test_rule9_hard_cap_not_applicable_above_2000_equity():
    # 16% of a $5,000 account would fail the small-account hard cap,
    # but equity >= $2,000 so rule 9 does not apply. Keep company/sector
    # exposure within their own caps (20% / 30%) so only rule 9 is exempt.
    result = call(
        trade=base_trade(cost=800.0),  # 16% of $5,000
        account_equity=5000.0,
        available_cash=5000.0,
    )
    checks_by_number = {c["number"]: c for c in result["checks"]}
    assert checks_by_number[9]["passed"] is True
    assert result["approved"] is True


def test_rule9_target_band_is_advisory_not_blocking():
    # 5% is below the 8-12% target band but still under the 15% hard cap;
    # must not be rejected for missing the target band.
    result = call(trade=base_trade(cost=15.0))
    assert result["approved"] is True
    checks_by_number = {c["number"]: c for c in result["checks"]}
    assert checks_by_number[9]["passed"] is True


def test_rule10_company_exposure_over_20_percent_rejected():
    result = call(trade=base_trade(cost=30.0), current_company_exposure_pct=15.0)
    assert result["approved"] is False
    assert 10 in failing_rule_numbers(result)


def test_rule13_reward_risk_below_2_rejected():
    # entry 10, stop 9 (risk 1), target 11.5 (reward 1.5) -> R:R 1.5
    result = call(trade=base_trade(entry=10.0, stop=9.0, target=11.5))
    assert result["approved"] is False
    assert 13 in failing_rule_numbers(result)
    assert result["calculated"]["reward_risk"] == 1.5


def test_rule14_cost_exceeds_available_cash_rejected():
    result = call(trade=base_trade(cost=30.0), available_cash=20.0)
    assert result["approved"] is False
    assert 14 in failing_rule_numbers(result)


def test_rule15_missing_catalyst_rejected():
    result = call(trade=base_trade(catalyst=None))
    assert result["approved"] is False
    assert 15 in failing_rule_numbers(result)

    result_blank = call(trade=base_trade(catalyst="   "))
    assert result_blank["approved"] is False
    assert 15 in failing_rule_numbers(result_blank)


def test_rule16_missing_stop_rejected():
    result = call(trade=base_trade(stop=None))
    assert result["approved"] is False
    assert 16 in failing_rule_numbers(result)
    assert result["calculated"]["reward_risk"] is None


def test_rule16_incoherent_levels_rejected():
    # stop above entry is not a valid long risk structure
    result = call(trade=base_trade(entry=10.0, stop=11.0, target=12.0))
    assert result["approved"] is False
    assert 16 in failing_rule_numbers(result)


def test_rule17_averaging_down_on_decline_rejected():
    result = call(trade=base_trade(averaging_down_on_decline=True))
    assert result["approved"] is False
    assert 17 in failing_rule_numbers(result)


def test_rule18_blocked_sector_rejected():
    result = call(trade=base_trade(sector="Energy"), blocked_sectors=["Energy"])
    assert result["approved"] is False
    assert 18 in failing_rule_numbers(result)


def test_rule18_non_blocked_sector_allowed():
    result = call(trade=base_trade(sector="Technology"), blocked_sectors=["Energy"])
    assert result["approved"] is True


# ---------------------------------------------------------------------------
# Output shape sanity checks
# ---------------------------------------------------------------------------

def test_result_contains_all_required_fields():
    result = call()
    assert "approved" in result
    assert "checks" in result
    assert "rejection_reasons" in result
    assert "calculated" in result
    calc = result["calculated"]
    assert "position_percentage" in calc
    assert "company_exposure_after_pct" in calc
    assert "sector_exposure_after_pct" in calc
    assert "reward_risk" in calc
    assert len(result["checks"]) == 18
