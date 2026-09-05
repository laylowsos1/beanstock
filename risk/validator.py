"""Beanstock deterministic risk engine.

Hard-coded rule evaluation for a single proposed trade against Beanstock's
risk rules (see CLAUDE.md / memory/TRADING-STRATEGY.md). This module makes
no network calls, places no orders, and connects to no broker. It is pure
arithmetic and boolean logic over the inputs it is given.

Rule numbers below match the numbering the project owner specified:

 1. Account must be PAPER/SIMULATED.
 2. Reject if live mode is detected.
 3. Stocks/fractional shares only.
 4. Reject options.
 5. Reject short selling.
 6. Reject margin use.
 7. Maximum 6 open positions after trade.
 8. Maximum 3 new trades per week.
 9. If equity < $2,000: hard initial position-size cap of 15%
    (target band 8-12% is advisory and does not block approval).
10. Absolute company exposure max 20%.
11. Sector exposure max 30%.
12. Candidate score minimum 75.
13. Reward:risk minimum 2.0.
14. Proposed cost cannot exceed available cash.
15. Catalyst or durable thesis required.
16. Entry, stop/invalidation, and target required (and must form a
    coherent long risk structure: stop < entry < target).
17. Reject averaging down solely because price declined.
18. Reject new trades in a sector blocked after two consecutive failures.
"""

from dataclasses import dataclass, field
from typing import Any, Optional

PAPER_MODES = {"PAPER", "SIMULATED", "PAPER/SIMULATED"}
LIVE_MODES = {"LIVE", "REAL", "LIVE/REAL"}

ALLOWED_INSTRUMENT_TYPES = {"stock", "fractional_share"}

MAX_OPEN_POSITIONS = 6
MAX_NEW_TRADES_PER_WEEK = 3
SMALL_ACCOUNT_EQUITY_THRESHOLD = 2000.0
SMALL_ACCOUNT_POSITION_HARD_CAP_PCT = 15.0
SMALL_ACCOUNT_POSITION_TARGET_LOW_PCT = 8.0
SMALL_ACCOUNT_POSITION_TARGET_HIGH_PCT = 12.0
MAX_COMPANY_EXPOSURE_PCT = 20.0
MAX_SECTOR_EXPOSURE_PCT = 30.0
MIN_CANDIDATE_SCORE = 75
MIN_REWARD_RISK = 2.0


@dataclass
class RuleCheck:
    number: int
    name: str
    passed: bool
    blocking: bool
    detail: str

    def as_dict(self) -> dict:
        return {
            "number": self.number,
            "name": self.name,
            "passed": self.passed,
            "blocking": self.blocking,
            "detail": self.detail,
        }


def _reward_risk_from_levels(entry: Optional[float], stop: Optional[float],
                              target: Optional[float]) -> Optional[float]:
    """Compute reward:risk for a long position from entry/stop/target.

    Returns None if the levels are missing or do not form a valid
    long risk structure (stop < entry < target).
    """
    if entry is None or stop is None or target is None:
        return None
    try:
        entry = float(entry)
        stop = float(stop)
        target = float(target)
    except (TypeError, ValueError):
        return None

    risk = entry - stop
    reward = target - entry
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk


def validate_trade(
    account_equity: float,
    available_cash: float,
    current_positions: int,
    trades_this_week: int,
    proposed_trade: dict,
    current_company_exposure_pct: float,
    current_sector_exposure_pct: float,
    blocked_sectors: Optional[list] = None,
    account_mode: str = "PAPER",
) -> dict:
    """Evaluate a single proposed trade against every Beanstock hard rule.

    proposed_trade is expected to carry:
        symbol: str
        instrument_type: "stock" | "fractional_share" | "option" | ...
        side: "long" | "short"
        uses_margin: bool
        cost: float                       -- dollar cost of the position
        sector: str
        candidate_score: float
        catalyst: str | None
        entry: float | None
        stop: float | None
        target: float | None
        averaging_down_on_decline: bool   -- True if this add exists solely
                                              because price fell

    current_company_exposure_pct / current_sector_exposure_pct are the
    percentages of equity already allocated to this company / this
    company's sector *before* the proposed trade.

    Returns a dict with: approved, checks, rejection_reasons, calculated.
    """
    blocked_sectors = blocked_sectors or []
    checks: list[RuleCheck] = []

    def add(number: int, name: str, passed: bool, detail: str, blocking: bool = True) -> None:
        checks.append(RuleCheck(number, name, passed, blocking, detail))

    mode = (account_mode or "").strip().upper()
    instrument_type = (proposed_trade.get("instrument_type") or "").strip().lower()
    side = (proposed_trade.get("side") or "long").strip().lower()
    uses_margin = bool(proposed_trade.get("uses_margin", False))
    cost = float(proposed_trade.get("cost", 0) or 0)
    sector = proposed_trade.get("sector")
    candidate_score = proposed_trade.get("candidate_score")
    catalyst = proposed_trade.get("catalyst")
    entry = proposed_trade.get("entry")
    stop = proposed_trade.get("stop")
    target = proposed_trade.get("target")
    averaging_down_on_decline = bool(proposed_trade.get("averaging_down_on_decline", False))

    # --- Rule 1: account must be PAPER/SIMULATED ---
    is_paper = mode in PAPER_MODES
    add(1, "account_is_paper_or_simulated", is_paper,
        f"account_mode='{account_mode}' must be one of {sorted(PAPER_MODES)}")

    # --- Rule 2: reject if live mode is detected ---
    is_live = mode in LIVE_MODES
    add(2, "live_mode_not_detected", not is_live,
        f"account_mode='{account_mode}' " + ("is LIVE" if is_live else "is not LIVE"))

    # --- Rule 3: stocks/fractional shares only ---
    is_equity_instrument = instrument_type in ALLOWED_INSTRUMENT_TYPES
    add(3, "instrument_is_stock_or_fractional_share", is_equity_instrument,
        f"instrument_type='{instrument_type}' must be one of {sorted(ALLOWED_INSTRUMENT_TYPES)}")

    # --- Rule 4: reject options ---
    is_option = instrument_type == "option"
    add(4, "not_an_option", not is_option,
        f"instrument_type='{instrument_type}'" + (" is an option" if is_option else ""))

    # --- Rule 5: reject short selling ---
    is_short = side == "short"
    add(5, "not_short_selling", not is_short, f"side='{side}'")

    # --- Rule 6: reject margin use ---
    add(6, "no_margin_used", not uses_margin, f"uses_margin={uses_margin}")

    # --- Rule 7: max 6 open positions after trade ---
    positions_after = int(current_positions) + 1
    positions_ok = positions_after <= MAX_OPEN_POSITIONS
    add(7, "max_open_positions", positions_ok,
        f"positions_after={positions_after}, max={MAX_OPEN_POSITIONS}")

    # --- Rule 8: max 3 new trades per week ---
    trades_after = int(trades_this_week) + 1
    trades_ok = trades_after <= MAX_NEW_TRADES_PER_WEEK
    add(8, "max_new_trades_per_week", trades_ok,
        f"trades_this_week_after={trades_after}, max={MAX_NEW_TRADES_PER_WEEK}")

    # --- position percentage (used by rules 9-11) ---
    position_pct = (cost / account_equity * 100) if account_equity else float("inf")

    # --- Rule 9: small-account initial position size hard cap ---
    small_account = account_equity < SMALL_ACCOUNT_EQUITY_THRESHOLD
    if small_account:
        size_ok = position_pct <= SMALL_ACCOUNT_POSITION_HARD_CAP_PCT
        in_target_band = (SMALL_ACCOUNT_POSITION_TARGET_LOW_PCT
                           <= position_pct
                           <= SMALL_ACCOUNT_POSITION_TARGET_HIGH_PCT)
        detail = (
            f"position={position_pct:.2f}% of equity "
            f"(target {SMALL_ACCOUNT_POSITION_TARGET_LOW_PCT}-"
            f"{SMALL_ACCOUNT_POSITION_TARGET_HIGH_PCT}%, "
            f"hard cap {SMALL_ACCOUNT_POSITION_HARD_CAP_PCT}%); "
            f"{'within target band' if in_target_band else 'outside target band (advisory only)'}"
        )
    else:
        size_ok = True
        detail = f"equity={account_equity} >= ${SMALL_ACCOUNT_EQUITY_THRESHOLD:.0f}; hard cap not applicable"
    add(9, "small_account_position_size_hard_cap", size_ok, detail)

    # --- Rule 10: absolute company exposure max 20% ---
    company_exposure_after = float(current_company_exposure_pct) + position_pct
    company_ok = company_exposure_after <= MAX_COMPANY_EXPOSURE_PCT
    add(10, "max_company_exposure", company_ok,
        f"company_exposure_after={company_exposure_after:.2f}%, max={MAX_COMPANY_EXPOSURE_PCT}%")

    # --- Rule 11: sector exposure max 30% ---
    sector_exposure_after = float(current_sector_exposure_pct) + position_pct
    sector_ok = sector_exposure_after <= MAX_SECTOR_EXPOSURE_PCT
    add(11, "max_sector_exposure", sector_ok,
        f"sector_exposure_after={sector_exposure_after:.2f}%, max={MAX_SECTOR_EXPOSURE_PCT}%")

    # --- Rule 12: candidate score minimum 75 ---
    score_ok = candidate_score is not None and candidate_score >= MIN_CANDIDATE_SCORE
    add(12, "min_candidate_score", score_ok,
        f"candidate_score={candidate_score}, min={MIN_CANDIDATE_SCORE}")

    # --- Rule 16 computed first: needed for reward:risk calc ---
    valid_levels = entry is not None and stop is not None and target is not None
    reward_risk = _reward_risk_from_levels(entry, stop, target)
    levels_ok = valid_levels and reward_risk is not None
    add(16, "entry_stop_target_defined", levels_ok,
        f"entry={entry}, stop={stop}, target={target} "
        f"(requires stop < entry < target)")

    # --- Rule 13: reward:risk minimum 2.0 ---
    rr_ok = reward_risk is not None and reward_risk >= MIN_REWARD_RISK
    add(13, "min_reward_risk", rr_ok,
        f"reward_risk={reward_risk if reward_risk is not None else 'undefined'}, "
        f"min={MIN_REWARD_RISK}")

    # --- Rule 14: proposed cost cannot exceed available cash ---
    cash_ok = cost <= float(available_cash)
    add(14, "cost_within_available_cash", cash_ok,
        f"cost={cost}, available_cash={available_cash}")

    # --- Rule 15: catalyst or durable thesis required ---
    has_catalyst = bool(catalyst and str(catalyst).strip())
    add(15, "catalyst_or_thesis_documented", has_catalyst,
        f"catalyst={catalyst!r}")

    # --- Rule 17: reject averaging down solely because price declined ---
    add(17, "not_averaging_down_on_decline_alone", not averaging_down_on_decline,
        f"averaging_down_on_decline={averaging_down_on_decline}")

    # --- Rule 18: reject new trades in a blocked sector ---
    sector_blocked = sector in blocked_sectors
    add(18, "sector_not_blocked", not sector_blocked,
        f"sector='{sector}', blocked_sectors={blocked_sectors}")

    blocking_failures = [c for c in checks if c.blocking and not c.passed]
    approved = len(blocking_failures) == 0

    rejection_reasons = [f"Rule {c.number} ({c.name}): {c.detail}" for c in blocking_failures]

    return {
        "approved": approved,
        "checks": [c.as_dict() for c in checks],
        "rejection_reasons": rejection_reasons,
        "calculated": {
            "position_percentage": round(position_pct, 4),
            "company_exposure_after_pct": round(company_exposure_after, 4),
            "sector_exposure_after_pct": round(sector_exposure_after, 4),
            "reward_risk": round(reward_risk, 4) if reward_risk is not None else None,
        },
    }
