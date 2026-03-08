"""Event Handlers — Two-Stage Processing (Preliminary → Confirmed).

Handles NormalizedEvents from Live Odds (preliminary) and
Live Score (confirmed) sources, updating EngineState accordingly.

Preliminary (Live Odds, <1s):
  - goal_detected → ob_freeze + precompute cache
  - score_rollback → rollback preliminary state
  - odds_spike → ob_freeze
  - period_change → engine phase transition

Confirmed (Live Score, 3-8s):
  - goal_confirmed → commit S, ΔS, cooldown (or VAR rollback)
  - red_card → commit X transition, cooldown
  - period_change → cross-check engine phase
  - match_finished → FINISHED
  - source_failure → emergency ob_freeze

Reference: phase3.md → Step 3.1 → Event Handlers
"""

from __future__ import annotations

import asyncio

from src.common.logging import get_logger
from src.common.types import NormalizedEvent
from src.engine.state_machine import (
    FIRST_HALF,
    FINISHED,
    HALFTIME,
    IDLE,
    PRELIMINARY_DETECTED,
    SECOND_HALF,
    EngineState,
    check_ob_freeze_release,
    commit_goal,
    commit_red_card,
    set_confirmed,
    set_false_alarm,
    set_preliminary,
    set_var_cancelled,
    start_cooldown,
    transition_to_finished,
    transition_to_halftime,
    transition_to_second_half,
)

log = get_logger(__name__)

# Cooldown duration after confirmed events (seconds)
COOLDOWN_SECONDS = 15


# ---------------------------------------------------------------------------
# Preliminary handlers (Live Odds WebSocket, <1s)
# ---------------------------------------------------------------------------

def handle_preliminary_goal(state: EngineState, event: NormalizedEvent) -> None:
    """Score change detected via Live Odds — enter preliminary state.

    1. Set ob_freeze + PRELIMINARY_DETECTED
    2. Infer scoring team from score diff
    3. Cache provisional state for 0ms confirmation
    """
    set_preliminary(state)

    if event.score is None:
        return

    preliminary_score = event.score

    # Infer scoring side
    if preliminary_score[0] > state.score[0]:
        scoring_team = "localteam"
    elif preliminary_score[1] > state.score[1]:
        scoring_team = "visitorteam"
    else:
        log.warning("preliminary_score_ambiguous", score=preliminary_score)
        return

    # Provisional ΔS
    preliminary_delta_S = preliminary_score[0] - preliminary_score[1]

    # Cache for reuse at confirmation
    state.preliminary_cache = {
        "score": preliminary_score,
        "delta_S": preliminary_delta_S,
        "scoring_team": scoring_team,
        "timestamp": event.timestamp,
    }

    log.info(
        "preliminary_goal_detected",
        current=state.score,
        preliminary=preliminary_score,
        team=scoring_team,
    )


def handle_score_rollback(state: EngineState, event: NormalizedEvent) -> None:
    """Score decreased in Live Odds — likely VAR cancellation.

    If in preliminary state, rollback immediately.
    """
    if state.event_state == PRELIMINARY_DETECTED:
        log.warning(
            "score_rollback_from_preliminary",
            cached_score=state.preliminary_cache.get("score"),
            new_score=event.score,
        )
        set_var_cancelled(state)
    else:
        log.warning("score_rollback_unexpected", event_state=state.event_state)


def handle_odds_spike(state: EngineState, event: NormalizedEvent) -> None:
    """Abrupt odds move without score change — set ob_freeze."""
    import time as _time

    state.ob_freeze = True
    state._ob_freeze_start = _time.time()
    state._ob_stable_ticks = 0
    log.warning("odds_spike_detected", delta=event.delta)


def handle_penalty_hint(state: EngineState, event: NormalizedEvent) -> None:
    """Penalty awarded inferred from 15-25% odds shift — ob_freeze.

    A penalty conversion rate of ~76% means odds shift significantly
    but not as dramatically as a confirmed goal. We freeze the order
    book and wait for Goalserve confirmation.
    """
    import time as _time

    state.ob_freeze = True
    state._ob_freeze_start = _time.time()
    state._ob_stable_ticks = 0
    log.warning(
        "penalty_hint_detected",
        delta=event.delta,
        team=event.extra.get("favored_team"),
    )


def handle_red_card_hint(state: EngineState, event: NormalizedEvent) -> None:
    """Red card inferred from 8-15% sustained odds shift — ob_freeze.

    A red card changes the Markov state X, affecting λ for the rest
    of the match. We freeze and wait for Goalserve to confirm the
    red card event, which will trigger commit_red_card().
    """
    import time as _time

    state.ob_freeze = True
    state._ob_freeze_start = _time.time()
    state._ob_stable_ticks = 0
    log.warning(
        "red_card_hint_detected",
        delta=event.delta,
        team=event.extra.get("team_hint"),
        sustained_ticks=event.extra.get("sustained_ticks"),
    )


def handle_var_review_hint(state: EngineState, event: NormalizedEvent) -> None:
    """VAR review inferred from rapid odds oscillation — ob_freeze.

    Multiple direction reversals within a short window indicate high
    uncertainty (VAR reviewing a goal/penalty/red card). We freeze
    the order book with extra caution — the outcome is unpredictable.
    """
    import time as _time

    state.ob_freeze = True
    state._ob_freeze_start = _time.time()
    state._ob_stable_ticks = 0
    log.warning(
        "var_review_hint_detected",
        reversals=event.extra.get("reversal_count"),
    )


def handle_penalty_missed_hint(
    state: EngineState, event: NormalizedEvent
) -> None:
    """Penalty missed/saved inferred from odds bounce-back.

    Odds returned toward pre-penalty levels, indicating the penalty
    was missed or saved. Release ob_freeze if it was set by a
    penalty_hint (the situation has resolved without a goal).
    """
    # Release freeze — the penalty situation resolved without scoring
    if state.ob_freeze:
        state.ob_freeze = False
        state._ob_stable_ticks = 0
    log.info(
        "penalty_missed_hint_detected",
        recovery_pct=event.extra.get("recovery_pct"),
    )


def handle_period_change_preliminary(
    state: EngineState, event: NormalizedEvent
) -> None:
    """Period change detected via Live Odds (preliminary)."""
    period = event.period or ""

    if period in ("Paused", "Half", "HT"):
        transition_to_halftime(state)
    elif period in ("2nd Half", "2nd"):
        transition_to_second_half(state)
    elif period in ("1st Half", "1st"):
        if state.engine_phase not in (FIRST_HALF, SECOND_HALF, FINISHED):
            from src.engine.state_machine import transition_to_first_half
            transition_to_first_half(state)


# ---------------------------------------------------------------------------
# Confirmed handlers (Live Score REST, 3-8s)
# ---------------------------------------------------------------------------

def handle_confirmed_goal(state: EngineState, event: NormalizedEvent) -> None:
    """Goal confirmed by Live Score — commit or VAR-cancel.

    1. If VAR-cancelled → rollback
    2. Commit score (S, ΔS)
    3. Check preliminary cache reuse
    4. Enter cooldown
    """
    # VAR cancellation
    if event.var_cancelled:
        set_var_cancelled(state)
        log.info("goal_var_cancelled")
        return

    # Commit score
    team = event.team or ""
    commit_goal(state, team)

    # Check preliminary cache reuse
    cache_hit = False
    if (state.preliminary_cache
            and state.preliminary_cache.get("delta_S") == state.delta_S):
        cache_hit = True
        log.info("preliminary_cache_reused")

    # State transition: confirmed → cooldown
    set_confirmed(state)

    log.info(
        "goal_confirmed",
        score=state.score,
        delta_S=state.delta_S,
        team=team,
        cache_hit=cache_hit,
    )


def handle_confirmed_red_card(state: EngineState, event: NormalizedEvent) -> None:
    """Red card confirmed by Live Score — commit Markov transition.

    1. Transition X
    2. Enter cooldown
    """
    team = event.team or ""
    old_X = state.X
    commit_red_card(state, team)

    # State transition
    set_confirmed(state)

    log.info(
        "red_card_confirmed",
        X_before=old_X,
        X_after=state.X,
        team=team,
    )


def handle_confirmed_period(state: EngineState, event: NormalizedEvent) -> None:
    """Period confirmed by Live Score — cross-check with Live Odds."""
    period = event.period or ""

    if period == "Halftime" and state.engine_phase != HALFTIME:
        log.warning("halftime_confirmed_late")
        transition_to_halftime(state)
    elif period == "2nd Half" and state.engine_phase != SECOND_HALF:
        transition_to_second_half(state)


def handle_match_finished(state: EngineState, event: NormalizedEvent) -> None:
    """Match finished — confirmed by Live Score."""
    transition_to_finished(state)


def handle_live_score_failure(state: EngineState) -> None:
    """5 consecutive Live Score failures — emergency freeze."""
    state.ob_freeze = True
    log.error("live_score_source_failure_freeze")


# ---------------------------------------------------------------------------
# Dispatcher — route NormalizedEvent to handler
# ---------------------------------------------------------------------------

def dispatch_live_odds_event(state: EngineState, event: NormalizedEvent) -> None:
    """Route a Live Odds WebSocket event to the appropriate handler.

    Handles events from both GoalserveLiveOddsSource and OddsApiLiveOddsSource.

    Event types from Odds-API classifier:
      - score_change_hint: consensus collapse >25% (likely goal)
      - penalty_hint: 15-25% odds shift (penalty awarded)
      - red_card_hint: 8-15% sustained shift (red card)
      - var_review_hint: rapid oscillation (VAR review in progress)
      - penalty_missed_hint: bounce-back after penalty (penalty missed/saved)
      - odds_spike: significant move, no specific pattern match
    """
    if event.type == "goal_detected":
        handle_preliminary_goal(state, event)
    elif event.type == "score_rollback":
        handle_score_rollback(state, event)
    elif event.type == "period_change":
        handle_period_change_preliminary(state, event)
    elif event.type == "odds_spike":
        handle_odds_spike(state, event)
    elif event.type == "score_change_hint":
        # Odds-API inferred goal from consensus odds collapse.
        # Treat as ob_freeze trigger — NOT a preliminary goal,
        # because we don't have actual score data from Odds-API.
        handle_odds_spike(state, event)
    elif event.type == "penalty_hint":
        handle_penalty_hint(state, event)
    elif event.type == "red_card_hint":
        handle_red_card_hint(state, event)
    elif event.type == "var_review_hint":
        handle_var_review_hint(state, event)
    elif event.type == "penalty_missed_hint":
        handle_penalty_missed_hint(state, event)
    elif event.type == "stoppage_entered":
        pass  # Handled by stoppage manager (Phase 3 later)
    elif event.type == "match_removed":
        pass  # Odds-API event deletion — informational only


def dispatch_live_score_event(state: EngineState, event: NormalizedEvent) -> None:
    """Route a Live Score REST event to the appropriate handler."""
    if event.type == "goal_confirmed":
        handle_confirmed_goal(state, event)
    elif event.type == "red_card":
        handle_confirmed_red_card(state, event)
    elif event.type == "period_change":
        handle_confirmed_period(state, event)
    elif event.type == "match_finished":
        handle_match_finished(state, event)
    elif event.type == "source_failure":
        handle_live_score_failure(state)
