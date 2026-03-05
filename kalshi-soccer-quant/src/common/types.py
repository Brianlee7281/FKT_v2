"""Shared data types used across all phases."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TradingMode(Enum):
    PAPER = "paper"
    LIVE = "live"


class EnginePhase(Enum):
    WAITING_FOR_KICKOFF = "WAITING_FOR_KICKOFF"
    FIRST_HALF = "FIRST_HALF"
    HALFTIME = "HALFTIME"
    SECOND_HALF = "SECOND_HALF"
    FINISHED = "FINISHED"


class EventState(Enum):
    IDLE = "IDLE"
    PRELIMINARY_DETECTED = "PRELIMINARY_DETECTED"
    CONFIRMED = "CONFIRMED"
    FALSE_ALARM = "FALSE_ALARM"
    VAR_CANCELLED = "VAR_CANCELLED"


class MarkovState(int, Enum):
    ELEVEN_V_ELEVEN = 0   # 11v11
    TEN_V_ELEVEN = 1      # 10v11 (home sent off)
    ELEVEN_V_TEN = 2      # 11v10 (away sent off)
    TEN_V_TEN = 3         # 10v10


class SignalDirection(Enum):
    BUY_YES = "BUY_YES"
    BUY_NO = "BUY_NO"
    HOLD = "HOLD"


class AlignmentStatus(Enum):
    ALIGNED = "ALIGNED"
    DIVERGENT = "DIVERGENT"
    UNAVAILABLE = "UNAVAILABLE"


# ---------------------------------------------------------------------------
# Core data structures
# ---------------------------------------------------------------------------

@dataclass
class NormalizedEvent:
    """Unified event representation from any data source."""
    type: str               # goal_detected, goal_confirmed, red_card, period_change, etc.
    source: str             # live_odds, live_score, kalshi
    confidence: str         # preliminary, confirmed
    timestamp: float
    score: tuple[int, int] | None = None
    team: str | None = None            # localteam / visitorteam
    var_cancelled: bool = False
    period: str | None = None
    delta: float | None = None          # odds delta (for odds_spike)
    minute: float | None = None
    scorer_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class IntervalRecord:
    """A time interval where lambda is constant (Phase 1)."""
    match_id: str
    t_start: float
    t_end: float
    state_X: int                    # Markov state {0,1,2,3}
    delta_S: int                    # Score difference (home - away)
    home_goal_times: list[float] = field(default_factory=list)
    away_goal_times: list[float] = field(default_factory=list)
    goal_delta_before: list[int] = field(default_factory=list)
    goal_is_owngoal: list[bool] = field(default_factory=list)
    T_m: float = 0.0
    is_halftime: bool = False
    alpha_1: float = 0.0
    alpha_2: float = 0.0


@dataclass
class PreMatchData:
    """Phase 2 data collection output."""
    # Lineups
    home_starting_11: list[str] = field(default_factory=list)
    away_starting_11: list[str] = field(default_factory=list)
    home_formation: str = ""
    away_formation: str = ""

    # Tier 2: player aggregate features
    home_player_agg: dict[str, float] = field(default_factory=dict)
    away_player_agg: dict[str, float] = field(default_factory=dict)

    # Tier 1: team rolling stats
    home_team_rolling: dict[str, float] = field(default_factory=dict)
    away_team_rolling: dict[str, float] = field(default_factory=dict)

    # Tier 3: odds features
    odds_features: dict[str, Any] = field(default_factory=dict)

    # Tier 4: context
    home_rest_days: int = 0
    away_rest_days: int = 0
    h2h_goal_diff: float = 0.0

    # Metadata
    match_id: str = ""
    kickoff_time: str = ""


@dataclass
class Signal:
    """Phase 4 trading signal output."""
    direction: str              # BUY_YES, BUY_NO, HOLD
    EV: float                   # Final EV after VWAP
    P_cons: float               # Directional conservative P
    P_kalshi: float             # VWAP effective price
    rough_qty: int = 0
    alignment_status: str = "UNAVAILABLE"
    kelly_multiplier: float = 0.6
    market_ticker: str = ""


@dataclass
class TradeLog:
    """Full trade record for DB persistence."""
    timestamp: float = 0.0
    match_id: str = ""
    market_ticker: str = ""
    direction: str = ""
    order_type: str = ""
    quantity_ordered: int = 0
    quantity_filled: int = 0
    limit_price: float = 0.0
    fill_price: float = 0.0
    P_true_at_order: float = 0.0
    P_true_cons_at_order: float = 0.0
    P_kalshi_at_order: float = 0.0
    P_kalshi_best_at_order: float = 0.0
    P_bet365_at_order: float = 0.0
    EV_adj: float = 0.0
    sigma_MC: float = 0.0
    pricing_mode: str = ""
    f_kelly: float = 0.0
    K_frac: float = 0.0
    alignment_status: str = ""
    kelly_multiplier: float = 0.0
    cooldown_active: bool = False
    ob_freeze_active: bool = False
    event_state: str = ""
    engine_phase: str = ""
    bankroll_before: float = 0.0
    bankroll_after: float = 0.0
    is_paper: bool = True
    paper_slippage: float = 0.0


@dataclass
class SanityResult:
    """Phase 2 Step 2.4 sanity check output."""
    verdict: str = "GO"             # GO | GO_WITH_CAUTION | HOLD | SKIP
    delta_match_winner: float = 0.0
    delta_over_under: float = 0.0
    warning: str | None = None


@dataclass
class GoalEvent:
    """Parsed goal from Goalserve Fixtures."""
    minute: float
    extra_min: float
    player_id: str
    player_name: str
    team: str               # localteam / visitorteam
    scoring_team: str       # resolved (flipped for own goals)
    is_penalty: bool = False
    is_owngoal: bool = False
    var_cancelled: bool = False


@dataclass
class RedCardEvent:
    """Parsed red card from Goalserve Fixtures."""
    minute: float
    extra_min: float
    player_id: str
    player_name: str
    team: str               # localteam / visitorteam


@dataclass
class MatchResult:
    """Parsed match result for DB storage."""
    match_id: str
    league_id: str
    date: str
    home_team: str
    away_team: str
    ft_score_h: int
    ft_score_a: int
    ht_score_h: int
    ht_score_a: int
    added_time_1: int
    added_time_2: int
    status: str
    summary: dict = field(default_factory=dict)
    stats: dict | None = None
    player_stats: dict | None = None
    odds: dict | None = None
    lineups: dict | None = None
