"""Tier 3: Odds Features.

Source: Goalserve Pregame Odds — 20+ bookmakers
Extract Pinnacle probabilities, market averages, and uncertainty.

Reference: phase1.md → Step 1.3 → Tier 3
"""

from __future__ import annotations

from typing import Any

import numpy as np

from src.goalserve.parsers import parse_odds


TIER3_FEATURES = [
    "pinnacle_home_prob",
    "pinnacle_draw_prob",
    "pinnacle_away_prob",
    "market_avg_home_prob",
    "market_avg_draw_prob",
    "market_avg_away_prob",
    "bookmaker_odds_std",
    "n_bookmakers",
]


def build_odds_features(bookmakers: list[dict]) -> dict[str, float]:
    """Build Tier 3 features from bookmaker odds.

    Delegates to the existing parse_odds function in goalserve.parsers,
    then ensures all expected keys are present.

    Args:
        bookmakers: List of bookmaker dicts from Goalserve Pregame Odds.

    Returns:
        Dict of feature name → value.
    """
    parsed = parse_odds(bookmakers)
    if not parsed:
        return {f: 0.0 for f in TIER3_FEATURES}

    return {f: float(parsed.get(f, 0.0)) for f in TIER3_FEATURES}
