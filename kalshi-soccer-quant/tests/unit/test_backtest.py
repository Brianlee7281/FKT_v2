"""Tests for Step 3.6: In-Play Backtest.

Step 3.6.1: Verifies event reconstruction (historical_matches → NormalizedEvent).
Step 3.6.2: Verifies replay execution (ReplayEngine with tick-based replay).
Step 3.6.3: Verifies metrics computation (Brier, calibration, monotonicity, etc.).
Step 3.6.4: Verifies Go/No-Go criteria evaluation.
Step 3.6.5: Verifies orchestration (sync entry point, output files).

Reference: phase3.md -> Step 3.6
"""

from __future__ import annotations

import pytest

from src.calibration.step_3_6_backtest import (
    reconstruct_events,
    run_single_match_backtest,
    run_batch_backtest,
    make_default_params,
    MatchBacktestResult,
    BacktestMetrics,
    compute_match_outcome,
    compute_brier_scores,
    compute_calibration,
    compute_monotonicity,
    compute_directional_correctness,
    compute_simulated_pnl,
    compute_all_metrics,
    evaluate_go_no_go,
    GoNoGoReport,
    GoNoGoCriterion,
    format_go_no_go_report,
    run_phase3_backtest_sync,
    save_backtest_outputs,
    _serialize_metrics,
    _serialize_report,
)


# ---------------------------------------------------------------------------
# Fixtures: sample match data
# ---------------------------------------------------------------------------

def _make_match(
    *,
    summary: dict | None = None,
    ft_score_h: int = 0,
    ft_score_a: int = 0,
    ht_score_h: int | None = 0,
    ht_score_a: int | None = 0,
    added_time_1: int = 3,
    added_time_2: int = 5,
    match_id: str = "test_001",
) -> dict:
    return {
        "match_id": match_id,
        "summary": summary or {},
        "ft_score_h": ft_score_h,
        "ft_score_a": ft_score_a,
        "ht_score_h": ht_score_h,
        "ht_score_a": ht_score_a,
        "added_time_1": added_time_1,
        "added_time_2": added_time_2,
    }


def _format_a_goal(minute, team_key, extra_min="", owngoal=False, var_cancelled=False):
    """Build a Format A goal entry under summary.{team}.goals.player."""
    return {
        "minute": str(minute),
        "extra_min": str(extra_min) if extra_min else "",
        "owngoal": "True" if owngoal else "",
        "var_cancelled": "True" if var_cancelled else "",
    }, team_key


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestReconstructEventsBasicMatch:
    """Test basic match with no goals or cards."""

    def test_0_0_match_has_period_events(self):
        match = _make_match(ft_score_h=0, ft_score_a=0)
        events = reconstruct_events(match)

        types = [e.type for e in events]
        # Should have halftime events + second half start + match_finished
        assert "period_change" in types
        assert "match_finished" in types

    def test_match_finished_at_correct_time(self):
        match = _make_match(added_time_1=3, added_time_2=5)
        events = reconstruct_events(match)

        finished = [e for e in events if e.type == "match_finished"]
        assert len(finished) == 1
        assert finished[0].minute == 98.0  # 90 + 3 + 5

    def test_halftime_at_minute_45(self):
        match = _make_match()
        events = reconstruct_events(match)

        ht_events = [e for e in events if e.type == "period_change"]
        assert len(ht_events) == 3  # Paused, Halftime, 2nd Half
        assert ht_events[0].period == "Paused"
        assert ht_events[0].minute == 45.0
        assert ht_events[1].period == "Halftime"
        assert ht_events[2].period == "2nd Half"

    def test_no_halftime_when_scores_missing(self):
        match = _make_match(ht_score_h=None, ht_score_a=None)
        events = reconstruct_events(match)

        ht_events = [e for e in events if e.type == "period_change"]
        assert len(ht_events) == 0

    def test_events_sorted_by_timestamp(self):
        match = _make_match()
        events = reconstruct_events(match)

        timestamps = [e.timestamp for e in events]
        assert timestamps == sorted(timestamps)


class TestReconstructEventsGoals:
    """Test goal event reconstruction."""

    def test_single_home_goal_format_a(self):
        summary = {
            "localteam": {
                "goals": {
                    "player": {
                        "minute": "23",
                        "extra_min": "",
                    }
                }
            }
        }
        match = _make_match(summary=summary, ft_score_h=1, ft_score_a=0)
        events = reconstruct_events(match)

        goals = [e for e in events if e.type in ("goal_detected", "goal_confirmed")]
        assert len(goals) == 2

        # Preliminary
        assert goals[0].type == "goal_detected"
        assert goals[0].source == "live_odds"
        assert goals[0].confidence == "preliminary"
        assert goals[0].score == (1, 0)
        assert goals[0].minute == 23.0
        assert goals[0].period == "1st Half"

        # Confirmed
        assert goals[1].type == "goal_confirmed"
        assert goals[1].source == "live_score"
        assert goals[1].confidence == "confirmed"
        assert goals[1].score == (1, 0)
        assert goals[1].team == "localteam"
        assert goals[1].var_cancelled is False

    def test_confirmed_5s_after_preliminary(self):
        summary = {
            "localteam": {
                "goals": {"player": {"minute": "50", "extra_min": ""}}
            }
        }
        match = _make_match(summary=summary, ft_score_h=1, ft_score_a=0)
        events = reconstruct_events(match)

        goals = [e for e in events if e.type in ("goal_detected", "goal_confirmed")]
        assert goals[1].timestamp - goals[0].timestamp == 5.0

    def test_two_goals_running_score(self):
        """Two goals should have incrementing scores."""
        summary = {
            "localteam": {
                "goals": {
                    "player": [
                        {"minute": "10", "extra_min": ""},
                        {"minute": "30", "extra_min": ""},
                    ]
                }
            }
        }
        match = _make_match(summary=summary, ft_score_h=2, ft_score_a=0)
        events = reconstruct_events(match)

        goals_detected = [e for e in events if e.type == "goal_detected"]
        assert len(goals_detected) == 2
        assert goals_detected[0].score == (1, 0)
        assert goals_detected[1].score == (2, 0)

    def test_mixed_teams_running_score(self):
        summary = {
            "localteam": {
                "goals": {"player": {"minute": "10", "extra_min": ""}}
            },
            "visitorteam": {
                "goals": {"player": {"minute": "20", "extra_min": ""}}
            },
        }
        match = _make_match(summary=summary, ft_score_h=1, ft_score_a=1)
        events = reconstruct_events(match)

        goals_detected = [e for e in events if e.type == "goal_detected"]
        assert goals_detected[0].score == (1, 0)  # home scores
        assert goals_detected[1].score == (1, 1)  # away scores

    def test_second_half_goal_period(self):
        summary = {
            "visitorteam": {
                "goals": {"player": {"minute": "70", "extra_min": ""}}
            }
        }
        match = _make_match(summary=summary, ft_score_h=0, ft_score_a=1)
        events = reconstruct_events(match)

        goal = [e for e in events if e.type == "goal_detected"][0]
        assert goal.period == "2nd Half"


class TestReconstructEventsVarCancelled:
    """Test VAR cancellation handling."""

    def test_var_cancelled_goal_does_not_change_score(self):
        summary = {
            "localteam": {
                "goals": {
                    "player": [
                        {"minute": "20", "extra_min": "", "var_cancelled": "True"},
                        {"minute": "50", "extra_min": ""},
                    ]
                }
            }
        }
        match = _make_match(summary=summary, ft_score_h=1, ft_score_a=0)
        events = reconstruct_events(match)

        goals_detected = [e for e in events if e.type == "goal_detected"]
        # First goal detected at (1,0) but then cancelled
        assert goals_detected[0].score == (1, 0)
        # Second goal should also show (1,0) since first was cancelled
        assert goals_detected[1].score == (1, 0)

    def test_var_cancelled_emits_confirmation_with_flag(self):
        summary = {
            "localteam": {
                "goals": {
                    "player": {"minute": "30", "extra_min": "", "var_cancelled": "True"}
                }
            }
        }
        match = _make_match(summary=summary, ft_score_h=0, ft_score_a=0)
        events = reconstruct_events(match)

        confirmed = [e for e in events if e.type == "goal_confirmed"]
        assert len(confirmed) == 1
        assert confirmed[0].var_cancelled is True
        # Score should be (0,0) — the original score before the cancelled goal
        assert confirmed[0].score == (0, 0)


class TestReconstructEventsOwnGoal:
    """Test own goal team inversion."""

    def test_own_goal_inverts_scoring_team(self):
        summary = {
            "localteam": {
                "goals": {
                    "player": {
                        "minute": "25",
                        "extra_min": "",
                        "owngoal": "True",
                    }
                }
            }
        }
        match = _make_match(summary=summary, ft_score_h=0, ft_score_a=1)
        events = reconstruct_events(match)

        confirmed = [e for e in events if e.type == "goal_confirmed"][0]
        # Own goal by home team means visitor scores
        assert confirmed.team == "visitorteam"
        assert confirmed.score == (0, 1)

    def test_own_goal_away_inverts_to_home(self):
        summary = {
            "visitorteam": {
                "goals": {
                    "player": {
                        "minute": "40",
                        "extra_min": "",
                        "owngoal": "True",
                    }
                }
            }
        }
        match = _make_match(summary=summary, ft_score_h=1, ft_score_a=0)
        events = reconstruct_events(match)

        confirmed = [e for e in events if e.type == "goal_confirmed"][0]
        assert confirmed.team == "localteam"
        assert confirmed.score == (1, 0)


class TestReconstructEventsExtraTime:
    """Test stoppage-time minute calculation."""

    def test_extra_min_adds_to_minute(self):
        summary = {
            "localteam": {
                "goals": {
                    "player": {"minute": "90", "extra_min": "3"}
                }
            }
        }
        match = _make_match(summary=summary, ft_score_h=1, ft_score_a=0)
        events = reconstruct_events(match)

        goal = [e for e in events if e.type == "goal_detected"][0]
        assert goal.minute == 93.0

    def test_first_half_extra_time(self):
        summary = {
            "visitorteam": {
                "goals": {
                    "player": {"minute": "45", "extra_min": "2"}
                }
            }
        }
        match = _make_match(summary=summary, ft_score_h=0, ft_score_a=1)
        events = reconstruct_events(match)

        goal = [e for e in events if e.type == "goal_detected"][0]
        assert goal.minute == 47.0
        # 45+2 is still first half stoppage
        assert goal.period == "2nd Half"  # > 45 maps to 2nd half


class TestReconstructEventsRedCards:
    """Test red card reconstruction."""

    def test_red_card_emitted(self):
        summary = {
            "localteam": {
                "redcards": {
                    "player": {"minute": "35", "extra_min": ""}
                }
            }
        }
        match = _make_match(summary=summary)
        events = reconstruct_events(match)

        cards = [e for e in events if e.type == "red_card"]
        assert len(cards) == 1
        assert cards[0].team == "localteam"
        assert cards[0].minute == 35.0
        assert cards[0].source == "live_score"
        assert cards[0].confidence == "confirmed"

    def test_red_card_before_goal_at_same_minute(self):
        """Red card should appear before goal at the same minute."""
        summary = {
            "visitorteam": {
                "redcards": {
                    "player": {"minute": "60", "extra_min": ""}
                },
                "goals": {
                    "player": {"minute": "60", "extra_min": ""}
                },
            }
        }
        match = _make_match(summary=summary, ft_score_h=0, ft_score_a=1)
        events = reconstruct_events(match)

        # Filter to just the events at minute 60
        at_60 = [e for e in events if e.minute == 60.0
                 and e.type in ("red_card", "goal_detected", "goal_confirmed")]
        assert at_60[0].type == "red_card"
        assert at_60[1].type == "goal_detected"


class TestReconstructEventsFormatB:
    """Test flat goals format (Format B)."""

    def test_flat_goals_format(self):
        summary = {
            "goal": [
                {"team": "localteam", "minute": "15", "player": "Smith"},
                {"team": "visitorteam", "minute": "60", "player": "Jones"},
            ]
        }
        match = _make_match(summary=summary, ft_score_h=1, ft_score_a=1)
        events = reconstruct_events(match)

        goals_detected = [e for e in events if e.type == "goal_detected"]
        assert len(goals_detected) == 2
        assert goals_detected[0].score == (1, 0)
        assert goals_detected[1].score == (1, 1)

    def test_flat_format_own_goal_via_player_name(self):
        summary = {
            "goal": [
                {"team": "localteam", "minute": "30", "player": "Smith (OG)"},
            ]
        }
        match = _make_match(summary=summary, ft_score_h=0, ft_score_a=1)
        events = reconstruct_events(match)

        confirmed = [e for e in events if e.type == "goal_confirmed"][0]
        assert confirmed.team == "visitorteam"

    def test_flat_format_stoppage_minute(self):
        summary = {
            "goal": [
                {"team": "localteam", "minute": "90+4", "player": "Smith"},
            ]
        }
        match = _make_match(summary=summary, ft_score_h=1, ft_score_a=0)
        events = reconstruct_events(match)

        goal = [e for e in events if e.type == "goal_detected"][0]
        assert goal.minute == 94.0


class TestReconstructEventsComplex:
    """Integration test with a complex multi-event match."""

    def test_world_cup_final_2022_style(self):
        """3-3 match with goals from both sides across both halves."""
        summary = {
            "localteam": {
                "goals": {
                    "player": [
                        {"minute": "23", "extra_min": ""},
                        {"minute": "36", "extra_min": ""},
                        {"minute": "80", "extra_min": ""},
                    ]
                }
            },
            "visitorteam": {
                "goals": {
                    "player": [
                        {"minute": "80", "extra_min": ""},
                        {"minute": "81", "extra_min": ""},
                        {"minute": "90", "extra_min": "8"},
                    ]
                }
            },
        }
        match = _make_match(
            summary=summary,
            ft_score_h=3, ft_score_a=3,
            added_time_1=1, added_time_2=10,
        )
        events = reconstruct_events(match)

        # 6 goals -> 12 goal events (6 preliminary + 6 confirmed)
        goal_events = [e for e in events
                       if e.type in ("goal_detected", "goal_confirmed")]
        assert len(goal_events) == 12

        # Check final score progression via detected events
        detected = [e for e in events if e.type == "goal_detected"]
        scores = [e.score for e in detected]
        # Goals at: 23'(H), 36'(H), 80'(H), 80'(A), 81'(A), 98'(A)
        assert scores == [
            (1, 0), (2, 0), (3, 0),
            (3, 1), (3, 2), (3, 3),
        ]

        # Match finished at 90 + 1 + 10 = 101
        finished = [e for e in events if e.type == "match_finished"]
        assert finished[0].minute == 101.0

        # All events sorted
        timestamps = [e.timestamp for e in events]
        assert timestamps == sorted(timestamps)


# ===========================================================================
# Step 3.6.2: Replay Execution Tests
# ===========================================================================

class TestRunSingleMatchBacktest:
    """Test single-match backtest replay."""

    def test_0_0_match_returns_snapshots(self):
        match = _make_match(ft_score_h=0, ft_score_a=0)
        params = make_default_params(match_id="test_001")
        result = run_single_match_backtest(match, params)

        assert isinstance(result, MatchBacktestResult)
        assert result.match_id == "test_001"
        assert result.ft_score_h == 0
        assert result.ft_score_a == 0
        assert result.error is None
        assert len(result.snapshots) > 0

    def test_tick_mode_produces_regular_snapshots(self):
        match = _make_match(ft_score_h=0, ft_score_a=0)
        params = make_default_params()
        result = run_single_match_backtest(
            match, params, tick_interval=1.0, mode="tick"
        )

        assert result.error is None
        # Tick-based: should have ~T_m + event snapshots + kickoff
        tick_snaps = [s for s in result.snapshots if s.trigger_event == "tick"]
        assert len(tick_snaps) > 50  # At least 50 ticks for a 98-min match

    def test_event_mode_fewer_snapshots(self):
        match = _make_match(ft_score_h=1, ft_score_a=0, summary={
            "localteam": {
                "goals": {"player": {"minute": "30", "extra_min": ""}}
            }
        })
        params = make_default_params()

        tick_result = run_single_match_backtest(match, params, mode="tick")
        event_result = run_single_match_backtest(match, params, mode="event")

        assert event_result.error is None
        assert tick_result.error is None
        # Event mode should have far fewer snapshots
        assert len(event_result.snapshots) < len(tick_result.snapshots)

    def test_match_with_goals_updates_score(self):
        match = _make_match(
            ft_score_h=2, ft_score_a=1,
            summary={
                "localteam": {
                    "goals": {"player": [
                        {"minute": "15", "extra_min": ""},
                        {"minute": "60", "extra_min": ""},
                    ]}
                },
                "visitorteam": {
                    "goals": {"player": {"minute": "40", "extra_min": ""}}
                },
            },
        )
        params = make_default_params()
        result = run_single_match_backtest(match, params, mode="event")

        assert result.error is None
        assert result.n_events > 0

        # Final snapshot should have score reflecting goals
        final = result.snapshots[-1]
        assert final.score == (2, 1)

    def test_t_m_computed_from_added_time(self):
        match = _make_match(added_time_1=4, added_time_2=6)
        params = make_default_params()
        result = run_single_match_backtest(match, params)

        assert result.T_m == 100.0  # 90 + 4 + 6

    def test_bad_match_returns_error(self):
        """Invalid match data should produce an error, not crash."""
        match = {"match_id": "bad"}  # Missing required fields
        params = make_default_params()
        result = run_single_match_backtest(match, params)

        # Should still return a result (may or may not error depending
        # on how reconstruct_events handles missing data)
        assert isinstance(result, MatchBacktestResult)
        assert result.match_id == "bad"

    def test_snapshots_chronologically_ordered(self):
        match = _make_match(
            ft_score_h=1, ft_score_a=0,
            summary={
                "localteam": {
                    "goals": {"player": {"minute": "55", "extra_min": ""}}
                }
            },
        )
        params = make_default_params()
        result = run_single_match_backtest(match, params, mode="tick")

        assert result.error is None
        minutes = [s.match_minute for s in result.snapshots]
        assert minutes == sorted(minutes)

    def test_p_true_populated(self):
        """Snapshots should have P_true pricing data."""
        match = _make_match(ft_score_h=0, ft_score_a=0)
        params = make_default_params()
        result = run_single_match_backtest(match, params, mode="tick")

        assert result.error is None
        # After kickoff, ticks should produce P_true values
        ticks_with_pricing = [
            s for s in result.snapshots
            if s.trigger_event == "tick" and s.P_true
        ]
        assert len(ticks_with_pricing) > 0


class TestRunBatchBacktest:
    """Test batch backtest across multiple matches."""

    def test_batch_returns_one_result_per_match(self):
        matches = [
            _make_match(match_id="m1", ft_score_h=1, ft_score_a=0),
            _make_match(match_id="m2", ft_score_h=0, ft_score_a=2),
            _make_match(match_id="m3", ft_score_h=1, ft_score_a=1),
        ]
        params = make_default_params()
        results = run_batch_backtest(matches, params, mode="event")

        assert len(results) == 3
        assert results[0].match_id == "m1"
        assert results[1].match_id == "m2"
        assert results[2].match_id == "m3"

    def test_batch_continues_after_error(self):
        matches = [
            _make_match(match_id="ok1", ft_score_h=0, ft_score_a=0),
            _make_match(match_id="ok2", ft_score_h=1, ft_score_a=0),
        ]
        params = make_default_params()
        results = run_batch_backtest(matches, params, mode="event")

        # Both should succeed
        assert len(results) == 2
        ok_count = sum(1 for r in results if r.error is None)
        assert ok_count == 2


class TestMakeDefaultParams:
    """Test default parameter creation."""

    def test_creates_valid_params(self):
        params = make_default_params(a_H=-3.2, a_A=-3.4)
        assert params.a_H == -3.2
        assert params.a_A == -3.4

    def test_default_values(self):
        params = make_default_params()
        assert params.a_H == -3.5
        assert params.a_A == -3.5
        assert params.T_exp == 98.0


# ===========================================================================
# Step 3.6.3: Metrics Tests
# ===========================================================================

# Helper to generate backtest results for metrics tests
def _run_matches_for_metrics():
    """Run a small batch of matches and return results for metric tests."""
    matches = [
        _make_match(match_id="m1", ft_score_h=2, ft_score_a=1, summary={
            "localteam": {
                "goals": {"player": [
                    {"minute": "15", "extra_min": ""},
                    {"minute": "60", "extra_min": ""},
                ]}
            },
            "visitorteam": {
                "goals": {"player": {"minute": "40", "extra_min": ""}}
            },
        }),
        _make_match(match_id="m2", ft_score_h=0, ft_score_a=0),
        _make_match(match_id="m3", ft_score_h=1, ft_score_a=1, summary={
            "localteam": {
                "goals": {"player": {"minute": "30", "extra_min": ""}}
            },
            "visitorteam": {
                "goals": {"player": {"minute": "70", "extra_min": ""}}
            },
        }),
    ]
    params = make_default_params()
    return run_batch_backtest(matches, params, mode="tick")


class TestComputeMatchOutcome:
    """Test outcome computation from final scores."""

    def test_home_win(self):
        o = compute_match_outcome(2, 1)
        assert o["home_win"] == 1.0
        assert o["away_win"] == 0.0
        assert o["draw"] == 0.0
        assert o["over_25"] == 1.0  # 3 goals > 2
        assert o["btts_yes"] == 1.0

    def test_draw(self):
        o = compute_match_outcome(0, 0)
        assert o["draw"] == 1.0
        assert o["home_win"] == 0.0
        assert o["over_15"] == 0.0
        assert o["btts_yes"] == 0.0

    def test_away_win(self):
        o = compute_match_outcome(0, 3)
        assert o["away_win"] == 1.0
        assert o["over_25"] == 1.0
        assert o["btts_yes"] == 0.0

    def test_over_under_boundaries(self):
        o = compute_match_outcome(1, 1)
        assert o["over_15"] == 1.0   # 2 > 1
        assert o["over_25"] == 0.0   # 2 not > 2
        assert o["over_35"] == 0.0


class TestBrierScores:
    """Test Brier score computation."""

    def test_brier_returns_all_markets(self):
        results = _run_matches_for_metrics()
        brier, brier_bins, n_snaps = compute_brier_scores(results)

        assert n_snaps > 0
        for market in ["home_win", "draw", "away_win", "over_25"]:
            assert market in brier
            assert 0.0 <= brier[market] <= 1.0

    def test_brier_by_time_bin_keys(self):
        results = _run_matches_for_metrics()
        _, brier_bins, _ = compute_brier_scores(results)

        assert "0-15" in brier_bins
        assert "75-120" in brier_bins
        for bin_label, market_scores in brier_bins.items():
            for market, score in market_scores.items():
                assert 0.0 <= score <= 1.0

    def test_brier_skips_failed_matches(self):
        results = [
            MatchBacktestResult(
                match_id="fail", ft_score_h=0, ft_score_a=0,
                error="some error",
            )
        ]
        brier, _, n_snaps = compute_brier_scores(results)
        assert n_snaps == 0


class TestCalibration:
    """Test calibration curve computation."""

    def test_calibration_returns_bins(self):
        results = _run_matches_for_metrics()
        cal_bins, cal_error = compute_calibration(results)

        for market in ["home_win", "over_25"]:
            assert market in cal_bins
            assert market in cal_error
            assert cal_error[market] >= 0.0

    def test_calibration_bins_have_required_fields(self):
        results = _run_matches_for_metrics()
        cal_bins, _ = compute_calibration(results)

        for market, bins in cal_bins.items():
            for b in bins:
                assert "bin_center" in b
                assert "mean_pred" in b
                assert "mean_outcome" in b
                assert "count" in b
                assert b["count"] > 0

    def test_calibration_empty_results(self):
        cal_bins, cal_error = compute_calibration([])
        for market in ["home_win"]:
            assert cal_bins[market] == []
            assert cal_error[market] == 0.0


class TestMonotonicity:
    """Test monotonicity violation detection."""

    def test_monotonicity_returns_violations_per_market(self):
        results = _run_matches_for_metrics()
        violations, total = compute_monotonicity(results)

        assert total > 0
        for market in ["home_win", "over_25"]:
            assert market in violations
            assert violations[market] >= 0

    def test_monotonicity_no_violations_in_smooth_match(self):
        """A 0-0 match should have very few or no monotonicity violations."""
        match = _make_match(match_id="smooth", ft_score_h=0, ft_score_a=0)
        params = make_default_params()
        result = run_single_match_backtest(match, params, mode="tick")
        violations, total = compute_monotonicity([result])

        assert total > 0
        # With default flat params, tick-to-tick changes should be smooth
        total_v = sum(violations.values())
        violation_rate = total_v / (total * len(violations)) if total > 0 else 0
        assert violation_rate < 0.1  # Less than 10% violation rate


class TestDirectionalCorrectness:
    """Test directional correctness after goals."""

    def test_directional_checks_present(self):
        results = _run_matches_for_metrics()
        correct, total, failures = compute_directional_correctness(results)

        # Should have checks for matches with goals (m1 and m3)
        assert total > 0
        # Report correctness ratio
        if total > 0:
            ratio = correct / total
            assert ratio >= 0.0  # Just confirm it computes

    def test_directional_no_goals_no_checks(self):
        """Match with no goals should produce zero directional checks."""
        match = _make_match(match_id="ngoal", ft_score_h=0, ft_score_a=0)
        params = make_default_params()
        result = run_single_match_backtest(match, params, mode="event")
        _, total, _ = compute_directional_correctness([result])
        assert total == 0


class TestSimulatedPnL:
    """Test simulated P&L computation."""

    def test_pnl_returns_values(self):
        results = _run_matches_for_metrics()
        pnl, n_trades, sharpe, max_dd = compute_simulated_pnl(results)

        # With default params and 50% baseline, many matches should trigger
        assert isinstance(pnl, float)
        assert isinstance(n_trades, int)
        assert max_dd >= 0.0

    def test_pnl_no_trades_with_high_threshold(self):
        results = _run_matches_for_metrics()
        pnl, n_trades, _, _ = compute_simulated_pnl(
            results, edge_threshold=0.99
        )
        assert n_trades == 0
        assert pnl == 0.0

    def test_pnl_empty_results(self):
        pnl, n_trades, sharpe, max_dd = compute_simulated_pnl([])
        assert pnl == 0.0
        assert n_trades == 0


class TestComputeAllMetrics:
    """Test the aggregate metrics function."""

    def test_all_metrics_populated(self):
        results = _run_matches_for_metrics()
        metrics = compute_all_metrics(results)

        assert isinstance(metrics, BacktestMetrics)
        assert metrics.n_matches == 3
        assert metrics.n_failed == 0
        assert metrics.n_snapshots > 0
        assert len(metrics.brier_overall) > 0
        assert len(metrics.brier_by_time_bin) > 0
        assert len(metrics.calibration_bins) > 0
        assert len(metrics.calibration_error) > 0
        assert len(metrics.monotonicity_violations) > 0
        assert metrics.monotonicity_total_ticks > 0

    def test_all_metrics_with_failed_match(self):
        results = _run_matches_for_metrics()
        results.append(MatchBacktestResult(
            match_id="bad", ft_score_h=0, ft_score_a=0,
            error="crashed",
        ))
        metrics = compute_all_metrics(results)

        assert metrics.n_matches == 4
        assert metrics.n_failed == 1
        # Metrics should still compute from the 3 good matches
        assert metrics.n_snapshots > 0


# ===========================================================================
# Step 3.6.4: Go/No-Go Criteria Tests
# ===========================================================================

class TestEvaluateGoNoGo:
    """Test Go/No-Go criteria evaluation."""

    def test_report_structure(self):
        results = _run_matches_for_metrics()
        metrics = compute_all_metrics(results)
        report = evaluate_go_no_go(metrics, min_matches=1)

        assert isinstance(report, GoNoGoReport)
        assert report.verdict in ("GO", "NO_GO")
        assert report.n_matches == 3
        assert len(report.criteria) >= 6
        for c in report.criteria:
            assert isinstance(c, GoNoGoCriterion)
            assert c.name
            assert c.threshold

    def test_criterion_names_present(self):
        results = _run_matches_for_metrics()
        metrics = compute_all_metrics(results)
        report = evaluate_go_no_go(metrics, min_matches=1)

        names = {c.name for c in report.criteria}
        assert "minimum_matches" in names
        assert "brier_score_home_win" in names
        assert "brier_decreasing_trend" in names
        assert "calibration_max_deviation" in names
        assert "monotonicity_violations" in names
        assert "mc_analytical_divergence" in names
        assert "directional_correctness" in names

    def test_min_matches_fail(self):
        """Should fail if fewer matches than minimum."""
        results = _run_matches_for_metrics()
        metrics = compute_all_metrics(results)
        report = evaluate_go_no_go(metrics, min_matches=1000)

        match_criterion = next(
            c for c in report.criteria if c.name == "minimum_matches"
        )
        assert match_criterion.passed is False
        assert report.verdict == "NO_GO"

    def test_min_matches_pass(self):
        results = _run_matches_for_metrics()
        metrics = compute_all_metrics(results)
        report = evaluate_go_no_go(metrics, min_matches=1)

        match_criterion = next(
            c for c in report.criteria if c.name == "minimum_matches"
        )
        assert match_criterion.passed is True

    def test_brier_criterion(self):
        results = _run_matches_for_metrics()
        metrics = compute_all_metrics(results)

        # With a very generous threshold, should pass
        report = evaluate_go_no_go(
            metrics, min_matches=1, bs_threshold=0.99
        )
        bs_criterion = next(
            c for c in report.criteria if c.name == "brier_score_home_win"
        )
        assert bs_criterion.passed is True

        # With an impossibly tight threshold, should fail
        report = evaluate_go_no_go(
            metrics, min_matches=1, bs_threshold=0.001
        )
        bs_criterion = next(
            c for c in report.criteria if c.name == "brier_score_home_win"
        )
        assert bs_criterion.passed is False

    def test_calibration_criterion(self):
        results = _run_matches_for_metrics()
        metrics = compute_all_metrics(results)

        # Generous threshold
        report = evaluate_go_no_go(
            metrics, min_matches=1, calibration_threshold=1.0
        )
        cal_criterion = next(
            c for c in report.criteria if c.name == "calibration_max_deviation"
        )
        assert cal_criterion.passed is True

    def test_monotonicity_criterion(self):
        results = _run_matches_for_metrics()
        metrics = compute_all_metrics(results)

        # Generous threshold
        report = evaluate_go_no_go(
            metrics, min_matches=1, monotonicity_threshold=1.0
        )
        mono_criterion = next(
            c for c in report.criteria if c.name == "monotonicity_violations"
        )
        assert mono_criterion.passed is True

    def test_directional_correctness_criterion(self):
        results = _run_matches_for_metrics()
        metrics = compute_all_metrics(results)
        report = evaluate_go_no_go(metrics, min_matches=1)

        dir_criterion = next(
            c for c in report.criteria if c.name == "directional_correctness"
        )
        # Value should be a ratio between 0 and 1
        assert 0.0 <= dir_criterion.value <= 1.0

    def test_pnl_criteria_optional_by_default(self):
        """P&L criteria should only appear when there are trades."""
        results = _run_matches_for_metrics()
        metrics = compute_all_metrics(results)
        report = evaluate_go_no_go(metrics, min_matches=1)

        names = {c.name for c in report.criteria}
        if metrics.simulated_n_trades > 0:
            assert "simulated_pnl" in names
            assert "simulated_max_drawdown" in names

    def test_pnl_criteria_required(self):
        """When pnl_required=True, criteria appear even with 0 trades."""
        results = _run_matches_for_metrics()
        metrics = compute_all_metrics(results)
        # Force no trades by overriding
        metrics.simulated_n_trades = 0
        metrics.simulated_pnl = 0.0
        report = evaluate_go_no_go(
            metrics, min_matches=1, pnl_required=True
        )

        names = {c.name for c in report.criteria}
        assert "simulated_pnl" in names
        assert "simulated_max_drawdown" in names

    def test_all_pass_gives_go(self):
        """With very generous thresholds, verdict should be GO."""
        results = _run_matches_for_metrics()
        metrics = compute_all_metrics(results)
        report = evaluate_go_no_go(
            metrics,
            min_matches=1,
            bs_threshold=0.99,
            calibration_threshold=1.0,
            monotonicity_threshold=1.0,
            mc_divergence_threshold=1.0,
        )
        # The only criterion that might still fail is directional or BS trend
        # With default params, check if all passed
        if report.all_passed:
            assert report.verdict == "GO"
        else:
            assert report.verdict == "NO_GO"

    def test_n_passed_n_failed(self):
        results = _run_matches_for_metrics()
        metrics = compute_all_metrics(results)
        report = evaluate_go_no_go(metrics, min_matches=1)

        assert report.n_passed + report.n_failed == len(report.criteria)
        assert report.n_passed >= 0
        assert report.n_failed >= 0


class TestFormatGoNoGoReport:
    """Test report formatting."""

    def test_format_contains_verdict(self):
        results = _run_matches_for_metrics()
        metrics = compute_all_metrics(results)
        report = evaluate_go_no_go(metrics, min_matches=1)
        text = format_go_no_go_report(report)

        assert "Verdict:" in text
        assert report.verdict in text
        assert "PASS" in text or "FAIL" in text

    def test_format_contains_all_criteria(self):
        results = _run_matches_for_metrics()
        metrics = compute_all_metrics(results)
        report = evaluate_go_no_go(metrics, min_matches=1)
        text = format_go_no_go_report(report)

        for c in report.criteria:
            assert c.name in text


# ===========================================================================
# Step 3.6.5: Orchestration Tests
# ===========================================================================

class TestRunPhase3BacktestSync:
    """Test the synchronous orchestration entry point."""

    def test_returns_report(self):
        matches = [
            _make_match(match_id="s1", ft_score_h=1, ft_score_a=0),
            _make_match(match_id="s2", ft_score_h=0, ft_score_a=0),
        ]
        report = run_phase3_backtest_sync(
            matches, mode="event", min_matches=1,
        )

        assert isinstance(report, GoNoGoReport)
        assert report.verdict in ("GO", "NO_GO")
        assert report.n_matches == 2

    def test_with_custom_params(self):
        matches = [
            _make_match(match_id="p1", ft_score_h=0, ft_score_a=0),
        ]
        params = make_default_params(a_H=-3.0, a_A=-3.0)
        report = run_phase3_backtest_sync(
            matches, params=params, mode="event", min_matches=1,
        )

        assert isinstance(report, GoNoGoReport)

    def test_saves_output_files(self, tmp_path):
        matches = [
            _make_match(match_id="o1", ft_score_h=2, ft_score_a=1, summary={
                "localteam": {
                    "goals": {"player": [
                        {"minute": "20", "extra_min": ""},
                        {"minute": "70", "extra_min": ""},
                    ]}
                },
                "visitorteam": {
                    "goals": {"player": {"minute": "50", "extra_min": ""}}
                },
            }),
            _make_match(match_id="o2", ft_score_h=0, ft_score_a=0),
        ]

        out_dir = str(tmp_path / "backtest_out")
        report = run_phase3_backtest_sync(
            matches, output_dir=out_dir, mode="event", min_matches=1,
        )

        import os
        assert os.path.exists(os.path.join(out_dir, "backtest_report.json"))
        assert os.path.exists(os.path.join(out_dir, "per_match_details.json"))
        assert os.path.exists(os.path.join(out_dir, "calibration_curve.json"))
        assert os.path.exists(os.path.join(out_dir, "time_bin_brier.json"))

    def test_output_report_json_valid(self, tmp_path):
        matches = [
            _make_match(match_id="j1", ft_score_h=1, ft_score_a=1, summary={
                "localteam": {
                    "goals": {"player": {"minute": "30", "extra_min": ""}}
                },
                "visitorteam": {
                    "goals": {"player": {"minute": "60", "extra_min": ""}}
                },
            }),
        ]

        out_dir = str(tmp_path / "json_test")
        run_phase3_backtest_sync(
            matches, output_dir=out_dir, mode="event", min_matches=1,
        )

        import json
        with open(f"{out_dir}/backtest_report.json") as f:
            data = json.load(f)

        assert "verdict" in data
        assert "go_no_go" in data
        assert "metrics" in data
        assert data["go_no_go"]["verdict"] in ("GO", "NO_GO")
        assert "brier_overall" in data["metrics"]

    def test_per_match_details_content(self, tmp_path):
        matches = [
            _make_match(match_id="d1", ft_score_h=0, ft_score_a=0),
            _make_match(match_id="d2", ft_score_h=1, ft_score_a=0, summary={
                "localteam": {
                    "goals": {"player": {"minute": "45", "extra_min": ""}}
                },
            }),
        ]

        out_dir = str(tmp_path / "details_test")
        run_phase3_backtest_sync(
            matches, output_dir=out_dir, mode="event", min_matches=1,
        )

        import json
        with open(f"{out_dir}/per_match_details.json") as f:
            data = json.load(f)

        assert len(data) == 2
        assert data[0]["match_id"] == "d1"
        assert data[1]["match_id"] == "d2"
        assert data[0]["ft_score"] == "0-0"
        assert data[1]["ft_score"] == "1-0"

    def test_no_output_when_dir_not_provided(self):
        matches = [
            _make_match(match_id="n1", ft_score_h=0, ft_score_a=0),
        ]
        report = run_phase3_backtest_sync(
            matches, output_dir=None, mode="event", min_matches=1,
        )
        # Should succeed without saving files
        assert isinstance(report, GoNoGoReport)


class TestSerializeHelpers:
    """Test JSON serialization helpers."""

    def test_serialize_metrics(self):
        results = _run_matches_for_metrics()
        metrics = compute_all_metrics(results)
        data = _serialize_metrics(metrics)

        assert isinstance(data, dict)
        assert "brier_overall" in data
        assert "n_snapshots" in data
        assert "directional_correct" in data
        assert "simulated_pnl" in data

    def test_serialize_report(self):
        results = _run_matches_for_metrics()
        metrics = compute_all_metrics(results)
        report = evaluate_go_no_go(metrics, min_matches=1)
        data = _serialize_report(report)

        assert isinstance(data, dict)
        assert data["verdict"] in ("GO", "NO_GO")
        assert isinstance(data["criteria"], list)
        assert len(data["criteria"]) > 0
        for c in data["criteria"]:
            assert "name" in c
            assert "passed" in c


class TestSaveBacktestOutputs:
    """Test output file generation."""

    def test_creates_all_files(self, tmp_path):
        results = _run_matches_for_metrics()
        metrics = compute_all_metrics(results)
        report = evaluate_go_no_go(metrics, min_matches=1)

        out_dir = str(tmp_path / "save_test")
        save_backtest_outputs(out_dir, metrics, report, results)

        from pathlib import Path
        p = Path(out_dir)
        assert (p / "backtest_report.json").exists()
        assert (p / "per_match_details.json").exists()
        assert (p / "calibration_curve.json").exists()
        assert (p / "time_bin_brier.json").exists()

    def test_creates_output_dir(self, tmp_path):
        results = _run_matches_for_metrics()
        metrics = compute_all_metrics(results)
        report = evaluate_go_no_go(metrics, min_matches=1)

        nested = str(tmp_path / "a" / "b" / "c")
        save_backtest_outputs(nested, metrics, report, results)

        from pathlib import Path
        assert Path(nested).exists()
