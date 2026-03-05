# Implementation Roadmap

## How to Read This Document

This roadmap is a **linear, dependency-ordered** sequence of work.
Each step produces outputs that the next step consumes.
Do not skip ahead — every step has a verification gate that must pass before proceeding.

The entire system consists of 6 documents totaling ~7,000 lines of design specification.
This roadmap translates those specs into concrete implementation tasks.

```
Documents:
├── phase1.md    (1,044 lines)  Offline Calibration
├── phase2.md    (953 lines)    Pre-Match Initialization
├── phase3.md    (1,342 lines)  Live Trading Engine
├── phase4.md    (1,287 lines)  Arbitrage & Execution
├── dashboard_design.md    (911 lines)    Dashboard
└── blueprint.md (1,481 lines) 24/7 Automation Blueprint
```

---

## Prerequisites

### Accounts & API Keys

| Service | What You Need | How to Get It |
|---------|--------------|---------------|
| **Goalserve** | Full Soccer Package API key | Contact sales, request 14-day free trial first |
| **Kalshi** | Trading API key + secret | Sign up at kalshi.com, enable API access |
| **Slack** | Webhook URL for alerts | Create Slack app → Incoming Webhooks |
| **Telegram** (optional) | Bot token + chat ID | @BotFather → /newbot |

### Server

| Requirement | Minimum | Recommended |
|-------------|---------|-------------|
| CPU | 2 vCPU | 4 vCPU |
| RAM | 4 GB | 8 GB |
| Storage | 50 GB SSD | 100 GB SSD |
| OS | Ubuntu 22.04 LTS | Ubuntu 24.04 LTS |
| Network | **Static IP** (Goalserve whitelist) | US East (near Kalshi) |
| Python | 3.11+ | 3.12 |

> **Static IP is critical.** Goalserve uses IP-based authentication.
> You must register your server's IP with Goalserve before any API calls work.

### Local Development Environment

You'll build and test locally first, then deploy to the server.

```
Local machine:
├── Python 3.11+
├── Docker Desktop (for Redis + PostgreSQL)
├── Node.js 18+ (for React dashboard)
├── Git
└── IDE (VS Code recommended)
```

---

## Phase 0: Foundation (Weeks 1–2)

**Goal:** Project skeleton boots up, infrastructure runs, Goalserve data flows into DB.

### Step 0.1: Project Scaffold

Create the folder structure from the blueprint:

```bash
mkdir -p kalshi-soccer-quant/{config,data/{parameters,cache},src/{common,goalserve,kalshi,calibration/{features},prematch,engine,trading,analytics,scheduler,data,alerts,dashboard/{api,frontend}},scripts,tests/{unit,integration,replay},docs,logs}

cd kalshi-soccer-quant
touch src/__init__.py src/{common,goalserve,kalshi,calibration,prematch,engine,trading,analytics,scheduler,data,alerts,dashboard}/__init__.py
touch src/calibration/features/__init__.py
touch src/dashboard/api/__init__.py
```

Initialize Python project:

```bash
python -m venv .venv
source .venv/bin/activate
pip install poetry
poetry init
```

Core dependencies:

```toml
# pyproject.toml [tool.poetry.dependencies]
python = "^3.11"

# Data & ML
numpy = "^1.26"
scipy = "^1.12"
pandas = "^2.2"
xgboost = "^2.0"
torch = "^2.2"
numba = "^0.59"

# Async & HTTP
httpx = "^0.27"
websockets = "^12.0"
aiohttp = "^3.9"

# Infrastructure
redis = "^5.0"
asyncpg = "^0.29"
sqlalchemy = "^2.0"
psycopg2-binary = "^2.9"

# Scheduling
apscheduler = "^3.10"

# Dashboard
fastapi = "^0.109"
uvicorn = "^0.27"

# Utilities
pyyaml = "^6.0"
python-dotenv = "^1.0"
structlog = "^24.1"
```

**Verification:** `poetry install` completes without errors.

### Step 0.2: Infrastructure

```yaml
# docker-compose.yml
version: '3.8'
services:
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    volumes: ["redis_data:/data"]

  postgres:
    image: timescale/timescaledb:latest-pg16
    ports: ["5432:5432"]
    environment:
      POSTGRES_USER: kalshi
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: kalshi
    volumes: ["pg_data:/var/lib/postgresql/data"]

volumes:
  redis_data:
  pg_data:
```

```bash
docker-compose up -d
psql -h localhost -U kalshi -d kalshi -f scripts/setup_db.sql
```

The `setup_db.sql` file contains all tables from the blueprint
(match_jobs, trade_logs, positions, daily_analytics, event_logs, tick_snapshots,
param_versions, historical_matches).

**Verification:**
- `redis-cli ping` → PONG
- `psql -h localhost -U kalshi -c "SELECT 1"` → OK
- All 8 tables exist: `\dt` confirms

### Step 0.3: Common Utilities

Build `src/common/` — the shared foundation everything else depends on:

```
src/common/
├── config.py          # SystemConfig: loads YAML + env vars
├── logging.py         # Structured logging (structlog)
├── redis_client.py    # Async Redis pub/sub wrapper
├── db_client.py       # Async PostgreSQL wrapper (asyncpg)
└── types.py           # Shared data types
```

**config.py** — Load `config/system.yaml` and merge with environment variables:

```python
class SystemConfig:
    def __init__(self, config_path="config/system.yaml"):
        with open(config_path) as f:
            raw = yaml.safe_load(f)
        self.goalserve_api_key = os.environ["GOALSERVE_API_KEY"]
        self.kalshi_api_key = os.environ["KALSHI_API_KEY"]
        self.trading_mode = raw["trading_mode"]
        self.target_leagues = raw["target_leagues"]
        # ... etc
```

**types.py** — Define all shared data structures referenced across phases:

```python
@dataclass
class NormalizedEvent: ...
@dataclass
class Signal: ...
@dataclass
class IntervalRecord: ...
@dataclass
class TradeLog: ...
@dataclass
class PreMatchData: ...
```

**Verification:**
- `SystemConfig("config/system.yaml")` loads without error
- Redis pub/sub round-trip works (publish → subscribe → receive)
- DB insert + select round-trip works

### Step 0.4: Goalserve REST Client

Build `src/goalserve/client.py` — the gateway to all Goalserve data:

```python
class GoalserveClient:
    """REST client for Goalserve Fixtures, Stats, and Odds APIs."""

    async def get_fixtures(self, league_id: str, date: str) -> List[dict]: ...
    async def get_match_stats(self, match_id: str) -> dict: ...
    async def get_odds(self, league_id: str) -> List[dict]: ...
```

Also build `src/goalserve/parsers.py` — transform Goalserve's raw JSON
into internal types:

```python
def parse_goals(summary: dict) -> List[GoalEvent]: ...
def parse_red_cards(summary: dict) -> List[RedCardEvent]: ...
def parse_player_stats(player_stats: dict) -> List[PlayerStats]: ...
def parse_team_stats(stats: dict) -> TeamStats: ...
def parse_odds(bookmakers: List[dict]) -> OddsFeatures: ...
```

**Verification:**
- Fetch today's fixtures for EPL → returns match list
- Fetch a completed match's stats → returns player-level data
- Fetch odds for a league → returns 20+ bookmakers
- `var_cancelled` field is present in goal events (check with trial data)
- `addedTime_period1/2` fields are present in matchinfo

> **CRITICAL CHECKS during Goalserve trial period (14 days):**
> 1. Does `summary.redcards` include second-yellow dismissals?
> 2. How far back does `player_stats` historical data go? (Need 3+ seasons)
> 3. Does the xG field exist in `stats`? What's the exact field name?
> 4. What's the stable match ID? (`static_id` vs `id`)
> 5. What are the exact `period` string values in Live Odds? ("1st Half" vs "1st" vs "First Half")
> 6. What are the exact market IDs? ("1777" for Fulltime Result — verify)
>
> **Document all findings.** These field mappings feed into every parser.

### Step 0.5: Historical Data Backfill

Build `src/data/collector.py` and run the initial backfill:

```python
class DataCollector:
    async def backfill_historical(self, league_id: str, seasons: List[str]):
        """One-time backfill of 3-5 seasons of historical data."""
        for season in seasons:
            fixtures = await self.goalserve.get_fixtures(league_id, season)
            for match in fixtures:
                await self.db.upsert_match_result(match)
                stats = await self.goalserve.get_match_stats(match["id"])
                if stats:
                    await self.db.upsert_match_stats(match["id"], stats)
            await asyncio.sleep(1)  # Respect rate limits
```

```bash
python -m src.data.collector --backfill --leagues 1204,1399 --seasons 2020,2021,2022,2023,2024
```

**Verification:**
- `SELECT COUNT(*) FROM historical_matches` → 3,000+ matches
- Spot-check 5 matches: goals, red cards, addedTime, player_stats all present
- No NULL `addedTime_period1` fields (or document which leagues lack them)

---

## Phase 1: Offline Calibration (Weeks 3–5)

**Goal:** Learn all MMPP parameters from historical data. This is the mathematical core.

### Step 1.1: Interval Splitting

**Reference:** phase1_goalserve_v1.md → Step 1.1

Build `src/calibration/step_1_1_intervals.py`:

```
Input:  historical_matches table (3,000+ matches)
Output: List[IntervalRecord] — hundreds of thousands of intervals
```

Key implementation details:
- Filter out `var_cancelled=True` goals
- Handle `owngoal=True` (invert scoring team, exclude from NLL point-event term)
- Parse `extra_min` for stoppage-time goals (minute=90, extra_min=3 → t=93)
- Calculate `T_m = 90 + addedTime_period1 + addedTime_period2`
- Exclude halftime interval from integration
- For each goal, record `delta_S_before` (score diff **before** the goal, not after)

```
tests/unit/test_intervals.py
├── test_basic_match_no_events()
├── test_single_goal_creates_two_intervals()
├── test_var_cancelled_goal_excluded()
├── test_own_goal_team_inversion()
├── test_red_card_state_transition()
├── test_halftime_excluded_from_integration()
├── test_added_time_T_m_calculation()
├── test_world_cup_final_2022()          ← complex match with 6 goals
└── test_delta_S_before_at_goal_time()   ← causal ordering
```

**Verification:** All tests pass. Run on full dataset, inspect 10 random matches manually.

### Step 1.2: Q Matrix Estimation

**Reference:** phase1_goalserve_v1.md → Step 1.2

Build `src/calibration/step_1_2_Q_matrix.py`:

```
Input:  IntervalRecords with X(t) state paths
Output: Q matrix (4×4), Q_off_normalized (4×4)
```

```
tests/unit/test_Q_matrix.py
├── test_no_red_cards_Q_near_zero()
├── test_single_red_card_counted()
├── test_diagonal_sum_zero()              ← q_ii = -Σ q_ij
├── test_Q_off_normalized_rows_sum_to_1()
└── test_additivity_state_3()
```

**Verification:** Red card rates are ~0.02-0.05 per 90 min (roughly 1 per 20-50 matches).

### Step 1.3: ML Prior (XGBoost)

**Reference:** phase1_goalserve_v1.md → Step 1.3

Build feature engineering pipeline:

```
src/calibration/features/
├── tier1_team.py       # Team rolling stats (xG, shots, possession, etc.)
├── tier2_player.py     # Player-level aggregation (rating, key passes, etc.)
├── tier3_odds.py       # Odds features (Pinnacle, market avg, std)
└── tier4_context.py    # Context (home/away, rest days, H2H)
```

Build `src/calibration/step_1_3_ml_prior.py`:

```
Input:  historical_matches + features
Output: XGBoost model (.xgb), feature_mask.json, median_values.json
```

- Target: goals per team per match
- Objective: `count:poisson`
- Feature selection: top 95% cumulative importance (gain-based)

> **Fallback:** If `player_stats` only goes back 2 seasons,
> train Tier 2 on recent data only. Older matches use Tier 1 + 3.

```
tests/unit/test_ml_prior.py
├── test_poisson_output_positive()
├── test_feature_mask_subset_of_full()
├── test_home_advantage_captured()
├── test_prediction_in_reasonable_range()   ← 0.5 < μ < 4.0
└── test_median_values_no_nans()
```

**Verification:** Mean predicted goals ≈ actual league average (~1.3-1.5 per team per match for EPL).

### Step 1.4: Joint NLL Optimization

**Reference:** phase1_goalserve_v1.md → Step 1.4

Build `src/calibration/step_1_4_nll.py` using PyTorch:

```python
class MMPPLoss(nn.Module):
    """
    Parameters: a_H[M], a_A[M], b[6], γ^H[2], γ^A[2], δ_H[4], δ_A[4]
    Total: 2M + 18 free parameters
    """
    ...
```

Key implementation:
- Multi-start (5-10 seeds), keep best
- Adam (lr=1e-3, 1000 epochs) → L-BFGS
- Clamping after each step (see Phase 1 doc clamping table)
- Own-goals excluded from Σ ln λ term

```
tests/unit/test_nll.py
├── test_nll_decreases_during_training()
├── test_b_clamped_within_bounds()          ← |b_i| ≤ 0.5
├── test_gamma_H_signs()                    ← γ^H_1 < 0, γ^H_2 > 0
├── test_gamma_A_signs()                    ← γ^A_1 > 0, γ^A_2 < 0
├── test_delta_zero_at_draw()               ← δ(0) = 0 fixed
├── test_delta_H_clamp_ranges()
├── test_delta_A_clamp_ranges()             ← v1 fix: δ_A bounds defined
├── test_own_goal_excluded_from_point_nll()
└── test_multi_start_best_selected()
```

**Verification:** γ signs match football intuition. b profile shows late-game intensity increase (b_6 > b_4).

### Step 1.5: Validation

**Reference:** phase1_goalserve_v1.md → Step 1.5

Build `src/calibration/step_1_5_validation.py`:

Walk-Forward CV. Train on seasons 1-3, validate on season 4. Repeat.

Metrics:
- Calibration plot (reliability diagram)
- Brier Score vs Pinnacle close line
- Multi-market cross-validation (1X2 + O/U 2.5 + BTTS)
- γ sign verification, δ LRT
- b half-ratio vs actual shots ratio
- Simulation P&L

**Go/No-Go criteria (all must pass):**

| Criterion | Threshold |
|-----------|-----------|
| Calibration | Diagonal ±5% |
| ΔBS vs Pinnacle | < 0 (model beats market) |
| Multi-market BS | All markets improved |
| Sim Max Drawdown | < 20% |
| All CV folds | Positive sim P&L |
| γ signs | All 4 correct |
| δ LRT | p < 0.05 (or drop δ) |

Save production parameters:

```
data/parameters/YYYYMMDD_HHMMSS/
├── params.json           ← b, γ^H, γ^A, δ_H, δ_A
├── Q.npy                 ← Q matrix (4×4)
├── xgboost.xgb           ← XGBoost weights
├── feature_mask.json      ← selected feature names
├── median_values.json     ← for missing value imputation
└── validation_report.json ← all metrics
```

Symlink: `data/parameters/production → ./YYYYMMDD_HHMMSS/`

**Verification:** All criteria pass. If any fail → iterate on Step 1.3 or 1.4.

---

## Phase 2: Pre-Match Pipeline (Weeks 5–6)

**Goal:** Given a match ID, automatically produce a ready-to-trade model instance.

### Step 2.1: Data Collection

**Reference:** phase2_goalserve_v1.md → Step 2.1

Build `src/prematch/step_2_1_data_collection.py`:

```
Input:  match_id
Output: PreMatchData (lineups, player rolling, team rolling, odds, context)
```

- Fetch lineup from Live Game Stats (60 min before kickoff)
- For each starting player, query last 5 matches from DB
- Aggregate by position group (FW/MF/DF/GK)
- Fetch team rolling stats and current pregame odds
- Same Goalserve IDs as Phase 1 — no mapping needed

```
tests/integration/test_data_collection.py
├── test_lineup_fetch_returns_22_players()
├── test_player_rolling_excludes_short_appearances()  ← mp < 10 filtered
├── test_odds_features_pinnacle_present()
├── test_feature_names_match_phase1_mask()             ← CRITICAL
└── test_prematch_data_no_none_fields()
```

### Step 2.2–2.3: Feature Selection + a Parameter

Build `src/prematch/step_2_2_feature_selection.py` and `step_2_3_a_parameter.py`:

```
Input:  PreMatchData + feature_mask.json + xgboost.xgb
Output: a_H, a_A, C_time
```

Apply mask → XGBoost inference → a = ln(μ̂) - ln(C_time).

**Verification:** a values in reasonable range (roughly -4 to -2).

### Step 2.4: Sanity Check

Build `src/prematch/step_2_4_sanity_check.py`:

Two-level: Match Winner vs Pinnacle (primary) + Over/Under cross-check (secondary).

**Verification:** Run against 20 past matches. GO for normal, SKIP for extreme outliers.

### Step 2.5: Initialization

Build `src/prematch/step_2_5_initialization.py`:

- Load Phase 1 params, compute P_grid + P_fine_grid
- Numba JIT warmup
- Set initial state: t=0, X=0, S=(0,0)

**Verification:** Model instance created. Numba warmup < 3s.

---

## Phase 3: Live Engine Core (Weeks 6–8)

**Goal:** Process real-time events and produce P_true every second.

### Step 3.1: Event Sources

**Reference:** phase3_goalserve_v1.md → Step 3.1

Build the two Goalserve sources:

```
src/goalserve/live_odds_source.py    # WebSocket, <1s
src/goalserve/live_score_source.py   # REST polling, 3s
```

```
tests/integration/test_live_odds_source.py
├── test_websocket_connects()
├── test_score_change_yields_event()
├── test_odds_spike_yields_event()
├── test_period_change_yields_event()
├── test_reconnect_on_disconnect()
└── test_score_rollback_yields_event()

tests/integration/test_live_score_source.py
├── test_poll_returns_data()
├── test_goal_diff_detected()
├── test_red_card_diff_detected()
├── test_5_failures_yields_source_failure()
└── test_var_cancelled_field_present()
```

**Verification:** Connect to a live match. Observe score change via WebSocket < 1s.

### Step 3.2: State Machine + Event Handlers

Build `src/engine/state_machine.py` and `src/engine/step_3_3_event_handler.py`:

```
IDLE → PRELIMINARY → CONFIRMED → COOLDOWN → IDLE
                   → FALSE_ALARM → IDLE
                   → VAR_CANCELLED → IDLE
```

```
tests/unit/test_event_handler.py
├── test_preliminary_sets_ob_freeze()
├── test_confirmed_goal_updates_S_and_deltaS()
├── test_confirmed_goal_applies_delta_H_and_A()
├── test_var_cancelled_rolls_back_state()
├── test_red_card_transitions_X_correctly()
├── test_red_card_applies_both_gamma_H_and_A()
├── test_cooldown_blocks_order_allowed()
├── test_false_alarm_after_timeout()
└── test_preliminary_cache_reused_on_confirm()
```

### Step 3.3: MC Core (Numba)

Build `src/engine/mc_core.py`:

```python
@njit(cache=True)
def mc_simulate_remaining(...):
    """Returns final_scores shape (N, 2). Must be < 1ms for N=50,000."""
```

```
tests/unit/test_mc_core.py
├── test_output_shape_N_by_2()
├── test_deterministic_with_same_seed()
├── test_scores_non_negative()
├── test_mean_matches_analytical_at_X0_dS0()
├── test_red_card_state_reduces_goals()
├── test_performance_under_1ms()               ← N=50000
└── test_delta_shifts_distribution()
```

### Step 3.4: Hybrid Pricing + Stoppage Time

Build `src/engine/step_3_4_pricing.py` and `src/engine/step_3_5_stoppage.py`.

### Step 3.5: Replay Engine

Build `tests/replay/replay_engine.py`:

```python
class ReplayEngine:
    """Replays a historical match through Phase 3 using stored events."""
    async def replay(self, match_id: str) -> List[Snapshot]: ...
```

**This is your primary debugging tool.** Use it constantly.

**Verification:** Replay 2022 World Cup Final. P_true changes correctly after all 6 goals.

---

## Phase 4: Execution Layer (Weeks 8–9)

**Goal:** Turn P_true into trades (paper first). All v2 fixes applied.

### Step 4.1: Kalshi Client + OrderBook

Build `src/kalshi/client.py` and `src/kalshi/orderbook.py`:

OrderBookSync must implement:
- `compute_vwap_buy(qty)` — ask-side VWAP
- `compute_vwap_sell(qty)` — bid-side VWAP

**Verification:** VWAP calculation matches manual computation on real orderbook.

### Step 4.2: Signal Generation (2-Pass VWAP)

**Reference:** phase4_goalserve_v2.md → Step 4.2

Build `src/trading/step_4_2_edge_detection.py`:

```
tests/unit/test_edge_detection.py
├── test_buy_yes_P_cons_is_lower_bound()       ← P_true - z*σ
├── test_buy_no_P_cons_is_upper_bound()        ← P_true + z*σ
├── test_vwap_pass2_reduces_EV()               ← final_EV ≤ rough_EV
├── test_alignment_ALIGNED_multiplier_0_8()    ← NOT 1.0
├── test_alignment_DIVERGENT_multiplier_0_5()
├── test_hold_when_vwap_kills_edge()           ← pass 2 gate
└── test_buy_no_EV_formula()                   ← (1-Pc)(1-c)P - Pc(1-P)
```

### Step 4.3: Kelly + Risk Limits

Build `src/trading/step_4_3_position_sizing.py` and `src/trading/risk_manager.py`:

```
tests/unit/test_kelly.py
├── test_buy_yes_W_L_correct()
├── test_buy_no_W_is_Pkalshi_times_1minusc()   ← W = (1-c)*P_kalshi
├── test_buy_no_L_is_1_minus_Pkalshi()          ← L = 1 - P_kalshi
├── test_alignment_multiplier_applied()
├── test_3_layer_limits_enforced()
└── test_match_cap_pro_rata()
```

### Step 4.4: Exit Logic (v2 Fixes)

**Reference:** phase4_goalserve_v2.md → Step 4.4

Build `src/trading/step_4_4_exit_logic.py`:

```
tests/unit/test_exit_logic.py

# ── v2 Fix #1: Edge Reversal Buy No ──
├── test_reversal_buy_no_uses_P_kalshi_bid()
│   → threshold = P_kalshi_bid + θ  (NOT 1-P_kalshi_bid + θ)
├── test_reversal_buy_no_fires_at_correct_level()
│   → bid=0.40, θ=0.02: fires when P_cons > 0.42 (NOT 0.62)

# ── v2 Fix #2: Expiry Eval Buy No ──
├── test_expiry_buy_no_E_hold_formula()
│   → E_hold = (1-Pc)(1-c)*entry - Pc*(1-entry)
├── test_expiry_buy_no_profitable_position_not_closed()
│   → entry=0.40, Pc=0.35: E_hold > 0, no exit triggered
├── test_expiry_buy_no_losing_position_closed()
│   → entry=0.40, Pc=0.65: E_hold < E_exit, exit triggered

# ── v2 Fix #3: bet365 Divergence Buy No ──
├── test_divergence_buy_no_uses_entry_price()
│   → threshold = entry_price + 0.05  (NOT 1-entry + 0.05)
├── test_divergence_buy_no_fires_at_5pp()
│   → entry=0.40: fires when P_bet365 > 0.45 (NOT 0.65)

# ── General ──
├── test_edge_decay_below_half_cent()
├── test_reversal_buy_yes_works()
└── test_divergence_buy_yes_works()
```

> **These are the most important unit tests in the entire system.**
> The v2 Buy No fixes prevent systematic money loss.
> Run them after every change to Step 4.4.

### Step 4.5: Execution (Paper v2)

Build `src/kalshi/execution.py`:

```
tests/unit/test_execution.py
├── test_paper_uses_vwap_not_best_ask()
├── test_paper_adds_1tick_slippage()
├── test_paper_partial_fill_when_depth_low()
└── test_paper_pnl_worse_than_naive_best_ask()  ← must be true
```

### Step 4.6: Settlement (v2 Fix)

Build `src/analytics/metrics.py`:

```
tests/unit/test_settlement.py
├── test_buy_yes_win_is_profit()     ← (1.00-0.45) × qty > 0  ✓
├── test_buy_yes_lose_is_loss()      ← (0.00-0.45) × qty < 0  ✓
├── test_buy_no_win_is_profit()      ← (0.40-0.00) × qty > 0  ✓  v2 fix
├── test_buy_no_lose_is_loss()       ← (0.40-1.00) × qty < 0  ✓  v2 fix
├── test_fee_only_on_positive()
└── test_buy_no_settlement_not_inverted()  ← explicitly verify no sign flip
```

---

## Phase 5: Automation + Dashboard (Weeks 9–12)

### Step 5.1: Match Engine

Build `src/engine/match_engine.py` — the main orchestrator:

```python
class MatchEngine:
    async def run_prematch(self) -> SanityResult: ...
    async def run_live(self): ...
```

```
tests/integration/test_match_engine.py
├── test_full_lifecycle_replay()
├── test_paper_trades_in_db()
├── test_crash_cancels_orders()
└── test_sanity_skip_stops_engine()
```

### Step 5.2: Scheduler

Build `src/scheduler/main.py`:

- Scan matches daily at 06:00 UTC
- Spawn engine 60 min before kickoff
- Monitor engine health every 10 seconds
- Clean up finished engines

```
tests/integration/test_scheduler.py
├── test_scan_finds_matches()
├── test_engine_spawned_on_time()
├── test_finished_engine_removed()
└── test_concurrent_engines()
```

### Step 5.3: Alert Service

Build `src/alerts/main.py`:

Test with real Slack webhook.

### Step 5.4: Dashboard (Minimal)

Build the Phase 0 essentials:

```
Must-have:
├── 1B: PriceChart.jsx      ← P_true vs P_kalshi vs P_bet365 (THE key chart)
├── 1A: MatchHeader.jsx      ← Score, minute, event_state
├── 1D: SignalPanel.jsx      ← Signals + paper positions
├── 1E: EventLog.jsx         ← Real-time events
├── 2B: PositionTable.jsx    ← Paper positions
└── 2C: PnLTimeline.jsx      ← Paper P&L
```

Backend: FastAPI + WebSocket → Redis subscriber.
Frontend: React + Recharts.

### Step 5.5: systemd Services

```bash
sudo systemctl enable kalshi-scheduler kalshi-dashboard kalshi-alerts kalshi-collector
```

**Verification:** Kill scheduler → restarts in 5 seconds.

---

## Phase 6: Paper Trading (Weeks 12–16)

### Weeks 12–13: Burn-In

- Deploy with `trading_mode: "paper"`
- Monitor every match through dashboard
- Watch for event timing, P_true trajectory, signal quality

### Weeks 13–14: Bug Fixing

Common bugs to expect:
- Timezone format mismatches
- Double-goal edge cases (two goals within seconds)
- WebSocket reconnection failures
- Memory leaks in long-running processes

### Weeks 14–16: Data Accumulation

Run unattended. Accumulate 50+ matches of paper data.

### Paper Go/No-Go

| Metric | Required |
|--------|----------|
| Paper trades | ≥ 50 |
| Paper P&L (after sim slippage) | > 0 |
| Preliminary accuracy | > 0.90 |
| Edge realization | 0.5 – 1.5 |
| System uptime | > 95% |
| No critical bugs for 1 week | Yes |

All pass → Phase A. Any fail → fix and extend.

---

## Phase 7: Live Trading (Week 16+)

### Phase A: Conservative

```yaml
trading_mode: "live"
K_frac: 0.25
z: 1.645
rapid_entry_enabled: false
```

Start with $500–1,000. Monitor every match for 2 weeks. Drawdown alert at 10%.

### Phase B: Adaptive (after 100+ trades)

Enable adaptive parameter adjustments. Allow DIVERGENT entries at 0.5x.

### Phase C: Mature (after 300+ trades)

K_frac up to 0.50. Conditionally enable Rapid Entry.

---

## Testing Strategy Summary

### Test Pyramid

```
                    ╱╲
                   ╱  ╲
                  ╱ E2E ╲           5 replay tests
                 ╱────────╲
                ╱Integration╲       20 tests
               ╱──────────────╲
              ╱   Unit Tests    ╲   80+ tests
             ╱────────────────────╲
```

### Most Critical Tests (Run After Every Change)

```
1. tests/unit/test_exit_logic.py         ← v2 Buy No fixes
2. tests/unit/test_settlement.py         ← v2 P&L direction
3. tests/unit/test_edge_detection.py     ← v2 VWAP + P_cons
4. tests/unit/test_mc_core.py            ← Performance + correctness
5. tests/unit/test_intervals.py          ← Data foundation
6. tests/unit/test_nll.py                ← Mathematical core
```

```bash
# Quick check (< 30s)
pytest tests/unit/ -v

# Full suite
pytest tests/ -v --tb=short
```

---

## Timeline Summary

| Week | Phase | Deliverable |
|------|-------|-------------|
| 1–2 | Foundation | Infra + Goalserve client + 3,000+ historical matches in DB |
| 3–5 | Phase 1 | Trained MMPP parameters (b, γ, δ, Q) passing all validation |
| 5–6 | Phase 2 | Pre-match pipeline: match_id → model instance |
| 6–8 | Phase 3 | Live engine: events → P_true every second |
| 8–9 | Phase 4 | Execution: signals → paper trades (v2 fixes verified) |
| 9–12 | Automation | Scheduler + Dashboard + Alerts + systemd |
| 12–16 | Paper | Full system on real matches, no real money |
| 16+ | Live | Phase A ($500) → B (adaptive) → C (mature) |

**First paper trade: ~Week 9.**
**First live trade: ~Week 16.**

---

## Appendix: Common Pitfalls

| Pitfall | Prevention |
|---------|-----------|
| Goalserve IP not whitelisted | Register static IP **before** starting trial |
| Timezone mismatches | Store everything UTC, convert only for display |
| Numba compilation fails on server | Pin numba+numpy versions, test on deploy target |
| WebSocket silently dies | Heartbeat check + auto-reconnect with backoff |
| Phase 1 overfits | Walk-forward CV — never skip Step 1.5 |
| Buy No formulas wrong | v2 unit tests catch all 3 direction bugs — run them always |
| VWAP not used in EV | 2-pass is inside generate_signal — verify `P_kalshi ≠ best_ask` |
| Paper P&L too optimistic | v2 Paper uses VWAP + slippage — verify `slippage > 0` |
| Parameter drift over time | Daily analytics detects Brier degradation → auto-recalibrate |
| Two goals in same second | Event handler must be idempotent per score state |
| Halftime pricing drift | engine_phase == HALFTIME freezes pricing and orders |
| Rapid Entry VAR risk | 5-second safety wait + P_cons z-correction (v2) |