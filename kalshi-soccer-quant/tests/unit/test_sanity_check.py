"""Tests for Step 2.4: Pre-Match Sanity Check.

Verifies primary (Match Winner vs Pinnacle), secondary (O/U),
and combined verdict logic.
"""

from __future__ import annotations

import pytest

from src.common.types import SanityResult
from src.prematch.step_2_4_sanity_check import (
    MARKET_AVG_CAUTION_THRESHOLD,
    OU_CONSISTENCY_THRESHOLD,
    PINNACLE_GO_THRESHOLD,
    PINNACLE_HOLD_THRESHOLD,
    primary_sanity_check,
    run_sanity_check,
    secondary_sanity_check,
)


# ---------------------------------------------------------------------------
# Helpers — build odds_features dicts
# ---------------------------------------------------------------------------

def _make_odds(
    pin_h: float = 0.45, pin_d: float = 0.28, pin_a: float = 0.27,
    mkt_h: float = 0.44, mkt_d: float = 0.29, mkt_a: float = 0.27,
    ou_over: float = 0.0, ou_under: float = 0.0,
) -> dict:
    return {
        "pinnacle_home_prob": pin_h,
        "pinnacle_draw_prob": pin_d,
        "pinnacle_away_prob": pin_a,
        "market_avg_home_prob": mkt_h,
        "market_avg_draw_prob": mkt_d,
        "market_avg_away_prob": mkt_a,
        "_ou_over_odds": ou_over,
        "_ou_under_odds": ou_under,
    }


# ---------------------------------------------------------------------------
# Primary check tests
# ---------------------------------------------------------------------------

class TestPrimarySanityCheck:
    def test_go_when_model_matches_pinnacle(self):
        """Model close to Pinnacle -> GO."""
        # mu_H=1.5, mu_A=1.2 gives roughly P(H)~0.40, P(D)~0.24, P(A)~0.36
        # Set Pinnacle close to those values
        pinnacle = (0.40, 0.25, 0.35)
        market = (0.40, 0.25, 0.35)
        verdict, delta_pin, _ = primary_sanity_check(1.5, 1.2, pinnacle, market)
        assert verdict == "GO"
        assert delta_pin < PINNACLE_GO_THRESHOLD

    def test_skip_when_huge_deviation(self):
        """Model far from Pinnacle -> SKIP."""
        # Model ~(0.40, 0.25, 0.35) vs Pinnacle (0.10, 0.10, 0.80)
        pinnacle = (0.10, 0.10, 0.80)
        market = (0.10, 0.10, 0.80)
        verdict, delta_pin, _ = primary_sanity_check(1.5, 1.2, pinnacle, market)
        assert verdict == "SKIP"
        assert delta_pin >= PINNACLE_HOLD_THRESHOLD

    def test_hold_when_moderate_deviation(self):
        """Model moderately deviates, market also deviates -> HOLD."""
        # Shift Pinnacle by ~0.20 from model
        pinnacle = (0.60, 0.20, 0.20)
        market = (0.60, 0.20, 0.20)
        verdict, delta_pin, delta_mkt = primary_sanity_check(1.5, 1.2, pinnacle, market)
        assert verdict == "HOLD"
        assert PINNACLE_GO_THRESHOLD <= delta_pin < PINNACLE_HOLD_THRESHOLD

    def test_go_with_caution_pinnacle_outlier(self):
        """Pinnacle deviates but market agrees with model -> GO_WITH_CAUTION."""
        # Model ~(0.44, 0.25, 0.30)
        pinnacle = (0.65, 0.15, 0.20)  # Pinnacle shifted ~0.21
        market = (0.44, 0.25, 0.31)    # Market close to model
        verdict, delta_pin, delta_mkt = primary_sanity_check(1.5, 1.2, pinnacle, market)
        assert verdict == "GO_WITH_CAUTION"
        assert delta_mkt < MARKET_AVG_CAUTION_THRESHOLD

    def test_delta_values_non_negative(self):
        pinnacle = (0.45, 0.28, 0.27)
        market = (0.44, 0.29, 0.27)
        _, delta_pin, delta_mkt = primary_sanity_check(1.5, 1.2, pinnacle, market)
        assert delta_pin >= 0
        assert delta_mkt >= 0


# ---------------------------------------------------------------------------
# Secondary check tests
# ---------------------------------------------------------------------------

class TestSecondarySanityCheck:
    def test_consistent_when_ou_matches(self):
        """Model O/U aligned with market -> consistent."""
        # mu_H=1.5, mu_A=1.2 -> total=2.7 -> P(over2.5) ~ 0.51
        # Set O/U odds implying ~50% over
        consistent, delta = secondary_sanity_check(1.5, 1.2, 2.0, 2.0)
        assert consistent is True
        assert delta < OU_CONSISTENCY_THRESHOLD

    def test_inconsistent_when_ou_diverges(self):
        """Model O/U diverges from market -> inconsistent."""
        # mu_H=0.5, mu_A=0.5 -> total=1.0 -> P(over2.5) ~ 0.08
        # Market implies ~70% over
        consistent, delta = secondary_sanity_check(0.5, 0.5, 1.43, 10.0)
        assert consistent is False
        assert delta >= OU_CONSISTENCY_THRESHOLD

    def test_no_ou_data_passes(self):
        """Missing O/U odds -> consistent by default."""
        consistent, delta = secondary_sanity_check(1.5, 1.2, 0.0, 0.0)
        assert consistent is True
        assert delta == 0.0

    def test_delta_non_negative(self):
        _, delta = secondary_sanity_check(1.5, 1.2, 1.90, 2.10)
        assert delta >= 0


# ---------------------------------------------------------------------------
# Combined verdict tests
# ---------------------------------------------------------------------------

class TestRunSanityCheck:
    def test_go_all_aligned(self):
        """Model matches Pinnacle and O/U -> GO."""
        odds = _make_odds(
            pin_h=0.40, pin_d=0.25, pin_a=0.35,
            mkt_h=0.40, mkt_d=0.25, mkt_a=0.35,
            ou_over=2.0, ou_under=2.0,
        )
        result = run_sanity_check(1.5, 1.2, odds)
        assert result.verdict == "GO"
        assert result.delta_match_winner < PINNACLE_GO_THRESHOLD

    def test_go_with_caution_ou_mismatch(self):
        """Model matches Pinnacle but O/U diverges -> GO_WITH_CAUTION."""
        odds = _make_odds(
            pin_h=0.40, pin_d=0.25, pin_a=0.35,
            mkt_h=0.40, mkt_d=0.25, mkt_a=0.35,
            ou_over=1.30, ou_under=4.00,  # implies ~75% over, model ~51%
        )
        result = run_sanity_check(1.5, 1.2, odds)
        assert result.verdict == "GO_WITH_CAUTION"
        assert result.warning is not None
        assert "O/U" in result.warning

    def test_skip_extreme_deviation(self):
        """Model wildly deviates from Pinnacle -> SKIP."""
        odds = _make_odds(
            pin_h=0.10, pin_d=0.10, pin_a=0.80,
            mkt_h=0.10, mkt_d=0.10, mkt_a=0.80,
        )
        result = run_sanity_check(1.5, 1.2, odds)
        assert result.verdict == "SKIP"

    def test_hold_moderate_deviation(self):
        """Model moderately deviates, both Pinnacle and market off -> HOLD."""
        odds = _make_odds(
            pin_h=0.60, pin_d=0.20, pin_a=0.20,
            mkt_h=0.60, mkt_d=0.20, mkt_a=0.20,
        )
        result = run_sanity_check(1.5, 1.2, odds)
        assert result.verdict == "HOLD"

    def test_hold_when_no_odds_data(self):
        """No odds data at all -> HOLD with warning."""
        result = run_sanity_check(1.5, 1.2, {})
        assert result.verdict == "HOLD"
        assert result.warning is not None

    def test_go_no_ou_data(self):
        """Match Winner aligns, no O/U data -> GO (ou defaults consistent)."""
        odds = _make_odds(
            pin_h=0.40, pin_d=0.25, pin_a=0.35,
            mkt_h=0.40, mkt_d=0.25, mkt_a=0.35,
        )
        result = run_sanity_check(1.5, 1.2, odds)
        assert result.verdict == "GO"

    def test_result_is_sanity_result(self):
        odds = _make_odds()
        result = run_sanity_check(1.5, 1.2, odds)
        assert isinstance(result, SanityResult)

    def test_deltas_populated(self):
        odds = _make_odds(ou_over=2.0, ou_under=2.0)
        result = run_sanity_check(1.5, 1.2, odds)
        assert result.delta_match_winner >= 0
        assert result.delta_over_under >= 0
