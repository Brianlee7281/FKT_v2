-- Kalshi Soccer Quant — Database Schema
-- Run: psql -h localhost -U kalshi -d kalshi -f scripts/setup_db.sql

-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- =============================================================
-- Table 1: match_jobs — Match schedule + status
-- =============================================================
CREATE TABLE IF NOT EXISTS match_jobs (
    match_id        TEXT PRIMARY KEY,
    league_id       TEXT NOT NULL,
    home_team       TEXT,
    away_team       TEXT,
    kickoff_time    TIMESTAMPTZ,
    status          TEXT DEFAULT 'SCHEDULED',  -- SCHEDULED/PREMATCH/LIVE/FINISHED/SKIPPED
    sanity_verdict  TEXT,
    engine_state    JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_match_jobs_status ON match_jobs(status);
CREATE INDEX IF NOT EXISTS idx_match_jobs_kickoff ON match_jobs(kickoff_time);

-- =============================================================
-- Table 2: trade_logs — Trade execution records (Phase 4 v2)
-- =============================================================
CREATE TABLE IF NOT EXISTS trade_logs (
    id              BIGSERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL,
    match_id        TEXT NOT NULL,
    market_ticker   TEXT NOT NULL,
    direction       TEXT NOT NULL,          -- BUY_YES | BUY_NO | SELL_YES | SELL_NO
    order_type      TEXT NOT NULL,          -- ENTRY | EXIT_EDGE_DECAY | EXIT_EDGE_REVERSAL
                                            -- | EXIT_EXPIRY_EVAL | EXIT_BET365_DIVERGENCE
                                            -- | RAPID_ENTRY
    quantity_ordered INT,
    quantity_filled  INT,
    limit_price     NUMERIC(6,4),
    fill_price      NUMERIC(6,4),
    P_true          NUMERIC(6,4),
    P_true_cons     NUMERIC(6,4),          -- Directional conservative P
    P_kalshi        NUMERIC(6,4),          -- VWAP effective price (v2)
    P_kalshi_best   NUMERIC(6,4),          -- Best ask/bid (for VWAP comparison)
    P_bet365        NUMERIC(6,4),
    EV_adj          NUMERIC(6,4),          -- Final EV after VWAP
    sigma_MC        NUMERIC(6,4),
    pricing_mode    TEXT,
    f_kelly         NUMERIC(6,4),
    K_frac          NUMERIC(4,2),
    alignment_status TEXT,                  -- ALIGNED | DIVERGENT | UNAVAILABLE
    kelly_multiplier NUMERIC(4,2),         -- 0.8 / 0.5 / 0.6
    cooldown_active BOOLEAN,
    ob_freeze_active BOOLEAN,
    event_state     TEXT,
    engine_phase    TEXT,
    bankroll_before NUMERIC(10,2),
    bankroll_after  NUMERIC(10,2),
    is_paper        BOOLEAN DEFAULT FALSE,
    paper_slippage  NUMERIC(6,4)           -- Simulated paper-mode slippage
);

CREATE INDEX IF NOT EXISTS idx_trade_logs_match ON trade_logs(match_id);
CREATE INDEX IF NOT EXISTS idx_trade_logs_time ON trade_logs(timestamp);

-- =============================================================
-- Table 3: positions — Open + settled positions (Phase 4 v2)
-- =============================================================
-- v2: realized_pnl uses direction-specific settlement formulas
--   Buy Yes: (settlement - entry_price) * quantity - fee
--   Buy No:  (entry_price - settlement) * quantity - fee
CREATE TABLE IF NOT EXISTS positions (
    id              BIGSERIAL PRIMARY KEY,
    match_id        TEXT NOT NULL,
    market_ticker   TEXT NOT NULL,
    direction       TEXT NOT NULL,          -- BUY_YES | BUY_NO
    entry_price     NUMERIC(6,4),          -- Yes-space price
    entry_time      TIMESTAMPTZ,
    quantity        INT,
    settlement      NUMERIC(6,4),          -- NULL if open, 1.00 or 0.00 at expiry
    realized_pnl    NUMERIC(10,2),         -- Directional settlement (v2)
    closed_at       TIMESTAMPTZ,
    is_paper        BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_positions_match ON positions(match_id);
CREATE INDEX IF NOT EXISTS idx_positions_open ON positions(settlement) WHERE settlement IS NULL;

-- =============================================================
-- Table 4: daily_analytics — Step 4.6 post-analysis output
-- =============================================================
CREATE TABLE IF NOT EXISTS daily_analytics (
    date            DATE PRIMARY KEY,
    brier_score     NUMERIC(6,4),
    delta_bs_pinnacle NUMERIC(6,4),
    edge_realization NUMERIC(6,4),
    max_drawdown_pct NUMERIC(6,4),
    bet365_alignment_value NUMERIC(6,4),   -- v2: market alignment value
    preliminary_accuracy NUMERIC(6,4),
    yes_edge_realization NUMERIC(6,4),
    no_edge_realization NUMERIC(6,4),
    total_trades    INT,
    total_pnl       NUMERIC(10,2),
    K_frac          NUMERIC(4,2),
    z               NUMERIC(4,2),
    param_version   TEXT
);

-- =============================================================
-- Table 5: event_logs — TimescaleDB hypertable for events
-- =============================================================
CREATE TABLE IF NOT EXISTS event_logs (
    time            TIMESTAMPTZ NOT NULL,
    match_id        TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    source          TEXT NOT NULL,
    confidence      TEXT,
    data            JSONB
);

SELECT create_hypertable('event_logs', 'time', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_event_logs_match ON event_logs(match_id, time DESC);

-- =============================================================
-- Table 6: tick_snapshots — TimescaleDB hypertable for ticks
-- =============================================================
CREATE TABLE IF NOT EXISTS tick_snapshots (
    time            TIMESTAMPTZ NOT NULL,
    match_id        TEXT NOT NULL,
    t               NUMERIC(6,2),
    score_h         INT,
    score_a         INT,
    state_x         INT,
    delta_s         INT,
    mu_h            NUMERIC(6,4),
    mu_a            NUMERIC(6,4),
    P_true          JSONB,
    P_kalshi        JSONB,
    P_bet365        JSONB,
    sigma_MC        NUMERIC(6,4),
    engine_phase    TEXT,
    event_state     TEXT
);

SELECT create_hypertable('tick_snapshots', 'time', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_tick_snapshots_match ON tick_snapshots(match_id, time DESC);

-- =============================================================
-- Table 7: param_versions — Phase 1 parameter versioning
-- =============================================================
CREATE TABLE IF NOT EXISTS param_versions (
    version         TEXT PRIMARY KEY,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    trigger_reason  TEXT,
    validation_report JSONB,
    is_production   BOOLEAN DEFAULT FALSE
);

-- =============================================================
-- Table 8: historical_matches — Phase 1 training data
-- =============================================================
CREATE TABLE IF NOT EXISTS historical_matches (
    match_id        TEXT PRIMARY KEY,
    league_id       TEXT,
    date            DATE,
    home_team       TEXT,
    away_team       TEXT,
    ft_score_h      INT,
    ft_score_a      INT,
    ht_score_h      INT,
    ht_score_a      INT,
    added_time_1    INT,
    added_time_2    INT,
    status          TEXT,
    summary         JSONB,       -- goals, redcards, yellowcards
    stats           JSONB,       -- team stats (shots, possession, etc.)
    player_stats    JSONB,       -- per-player stats
    odds            JSONB,       -- pregame odds (20+ bookmakers)
    lineups         JSONB,       -- formations + starting 11
    collected_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_historical_league ON historical_matches(league_id, date);
CREATE INDEX IF NOT EXISTS idx_historical_date ON historical_matches(date);
