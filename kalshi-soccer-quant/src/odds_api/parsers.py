"""Parse Odds-API response format into internal types.

Odds-API returns odds in a different structure than Goalserve:

    {
      "bookmakers": {
        "Bet365": [
          {"name": "ML", "updatedAt": "...", "odds": [{"home": "2.10", "draw": "3.40", "away": "3.20"}]},
          {"name": "Asian Handicap", "odds": [{"hdp": -0.5, "home": "1.95", "away": "1.85"}]},
          {"name": "Totals", "odds": [{"hdp": 2.5, "over": "1.90", "under": "1.90"}]}
        ]
      }
    }

This module converts that into the same feature dict that parse_odds() produces,
so Tier 3 features work identically regardless of data source.
"""

from __future__ import annotations

from typing import Any

import numpy as np


def parse_odds_api_response(event_data: dict) -> dict:
    """Parse Odds-API /odds response into normalized probability features.

    Produces the same output format as goalserve.parsers.parse_odds():
        pinnacle_home_prob, pinnacle_draw_prob, pinnacle_away_prob,
        market_avg_home_prob, market_avg_draw_prob, market_avg_away_prob,
        bookmaker_odds_std, n_bookmakers

    Args:
        event_data: Full response from GET /odds or an element of /odds/multi.

    Returns:
        Dict of Tier 3 feature name → value, or empty dict if no valid odds.
    """
    bookmakers = event_data.get("bookmakers", {})
    if not bookmakers:
        return {}

    all_probs: list[tuple[float, float, float]] = []
    pinnacle_prob: tuple[float, float, float] | None = None

    for bm_name, markets in bookmakers.items():
        # Find the ML (Match Result) market
        ml_market = _find_market(markets, "ML")
        if ml_market is None:
            continue

        odds_list = ml_market.get("odds", [])
        if not odds_list:
            continue

        # ML odds: {"home": "2.10", "draw": "3.40", "away": "3.20"}
        odds = odds_list[0]
        home_odds = _safe_float(odds.get("home"))
        draw_odds = _safe_float(odds.get("draw"))
        away_odds = _safe_float(odds.get("away"))

        if not all([home_odds > 0, draw_odds > 0, away_odds > 0]):
            continue

        prob = _remove_overround(home_odds, draw_odds, away_odds)
        all_probs.append(prob)

        # Use sharpest available book for "pinnacle_*" features.
        # Pinnacle is not on Odds-API; 1xbet is the sharpest alternative
        # (fastest line mover, lowest margins among available books).
        bm_lower = bm_name.lower()
        if "pinnacle" in bm_lower or "1xbet" in bm_lower:
            pinnacle_prob = prob

    if not all_probs:
        return {}

    avg_probs = tuple(np.mean(all_probs, axis=0))

    if pinnacle_prob is None:
        pinnacle_prob = avg_probs

    return {
        "pinnacle_home_prob": pinnacle_prob[0],
        "pinnacle_draw_prob": pinnacle_prob[1],
        "pinnacle_away_prob": pinnacle_prob[2],
        "market_avg_home_prob": avg_probs[0],
        "market_avg_draw_prob": avg_probs[1],
        "market_avg_away_prob": avg_probs[2],
        "bookmaker_odds_std": float(np.std([p[0] for p in all_probs])),
        "n_bookmakers": len(all_probs),
    }


def parse_ws_odds_update(msg: dict) -> dict | None:
    """Parse a WebSocket 'updated' message into ML odds per bookmaker.

    Args:
        msg: WebSocket message with type='updated'.

    Returns:
        Dict with keys: bookie, home_odds, draw_odds, away_odds, timestamp.
        None if message doesn't contain ML odds.
    """
    if msg.get("type") != "updated":
        return None

    markets = msg.get("markets", [])
    ml_market = _find_market(markets, "ML")
    if ml_market is None:
        return None

    odds_list = ml_market.get("odds", [])
    if not odds_list:
        return None

    odds = odds_list[0]
    home = _safe_float(odds.get("home"))
    draw = _safe_float(odds.get("draw"))
    away = _safe_float(odds.get("away"))

    if not all([home > 0, draw > 0, away > 0]):
        return None

    return {
        "bookie": msg.get("bookie", ""),
        "home_odds": home,
        "draw_odds": draw,
        "away_odds": away,
        "timestamp": msg.get("timestamp", ""),
        "event_id": msg.get("id", ""),
    }


def parse_asian_handicap(event_data: dict) -> list[dict]:
    """Extract Asian Handicap odds from all bookmakers.

    Returns list of dicts: {bookie, hdp, home, away}.
    """
    bookmakers = event_data.get("bookmakers", {})
    results = []

    for bm_name, markets in bookmakers.items():
        ah_market = _find_market(markets, "Asian Handicap")
        if ah_market is None:
            continue
        for odds in ah_market.get("odds", []):
            results.append({
                "bookie": bm_name,
                "hdp": _safe_float(odds.get("hdp")),
                "home": _safe_float(odds.get("home")),
                "away": _safe_float(odds.get("away")),
            })

    return results


def parse_totals(event_data: dict) -> list[dict]:
    """Extract Over/Under totals from all bookmakers.

    Returns list of dicts: {bookie, line, over, under}.
    """
    bookmakers = event_data.get("bookmakers", {})
    results = []

    for bm_name, markets in bookmakers.items():
        totals_market = _find_market(markets, "Totals")
        if totals_market is None:
            continue
        for odds in totals_market.get("odds", []):
            results.append({
                "bookie": bm_name,
                "line": _safe_float(odds.get("hdp")),
                "over": _safe_float(odds.get("over")),
                "under": _safe_float(odds.get("under")),
            })

    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_market(markets: list[dict] | dict, name: str) -> dict | None:
    """Find a market by name in the markets list.

    Handles both list format (WebSocket) and dict format (REST).
    """
    if isinstance(markets, dict):
        # REST format: markets is already a dict with bookmaker as key
        # This shouldn't happen at this level, but handle gracefully
        return None

    for m in markets:
        if m.get("name", "").upper() == name.upper():
            return m
    return None


def _remove_overround(h: float, d: float, a: float) -> tuple[float, float, float]:
    """Remove bookmaker overround from decimal odds."""
    total = 1.0 / h + 1.0 / d + 1.0 / a
    return (1.0 / h) / total, (1.0 / d) / total, (1.0 / a) / total


def _safe_float(val: Any) -> float:
    if val is None or val == "":
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0
