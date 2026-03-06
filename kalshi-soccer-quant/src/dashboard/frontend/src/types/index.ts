/** Core data types for the dashboard, matching backend state snapshots. */

// ---------------------------------------------------------------------------
// Match State (from WebSocket /ws/live/{match_id})
// ---------------------------------------------------------------------------

export interface MatchState {
  match_id: string;
  home: string;
  away: string;
  status: string;
  lifecycle: string | null;

  // Engine state fields
  engine_phase?: string;   // PRE_MATCH, FIRST_HALF, HALFTIME, SECOND_HALF, FINISHED
  event_state?: string;    // IDLE, PRELIMINARY, CONFIRMED, VAR_CANCELLED
  t?: number;              // match minute (decimal)
  score_h?: number;
  score_a?: number;
  X?: number;              // player count state (0=11v11, 1=10v11, etc.)
  cooldown?: boolean;
  ob_freeze?: boolean;

  // Pricing (populated by engine state snapshots)
  P_true?: Record<string, number>;
  P_kalshi_bid?: number;
  P_kalshi_ask?: number;
  P_bet365?: Record<string, number>;
  mu_H?: number;
  mu_A?: number;
  sigma_MC?: number;
  pricing_mode?: string;   // ANALYTICAL or MONTE_CARLO

  // Signal (single-market, legacy)
  EV?: number;
  direction?: string;
  bet365_confidence?: string;

  // Per-market signals (populated when engine computes signals)
  signals?: MarketSignal[];

  // Open positions for this match
  positions?: MatchPosition[];

  // Data source status (populated by engine)
  sources?: SourceStatusInfo[];

  // Engine meta
  trade_count?: number;
  bankroll?: number;
}

/** Data source connection status. */
export interface SourceStatusInfo {
  name: string;           // e.g. "Live Odds WS", "Kalshi WS", "Live Score"
  type: string;           // "websocket" or "polling"
  connected: boolean;
  last_message_ts?: number;  // Unix timestamp of last received message
  error?: string;
}

/** Per-market signal from the engine's current tick. */
export interface MarketSignal {
  market: string;
  direction: string;     // BUY_YES, BUY_NO, HOLD
  EV: number;
  alignment: string;     // aligned, divergent, unknown
  suggested_qty: number;
}

/** Open position for a specific match. */
export interface MatchPosition {
  market: string;
  direction: string;
  entry_price: number;
  current_price: number;
  quantity: number;
  pnl: number;
  bet365_aligned: boolean;
}

// ---------------------------------------------------------------------------
// Portfolio (from WebSocket /ws/portfolio and REST /api/portfolio/*)
// ---------------------------------------------------------------------------

export interface PortfolioSummary {
  trading_mode: string;
  bankroll: number;
  active_matches: number;
  open_positions: number;
  total_exposure: number;
  total_exposure_pct: number;
  unrealized_pnl: number;
  realized_pnl: number;
  risk_limits: RiskLimits;
}

export interface RiskLimits {
  l1_order_cap: number;
  l2_match_cap: number;
  l3_total_cap: number;
  l3_used_pct: number;
}

export interface Position {
  match_id: string;
  home: string;
  away: string;
  market: string;
  direction: string;
  entry_price: number;
  current_price?: number;
  quantity: number;
  pnl?: number;
  entry_time: number;
  status: string;           // open, settled_win, settled_loss
  bet365_aligned?: boolean;
}

export interface PnlPoint {
  timestamp: number;
  match_id: string;
  pnl: number;
  cumulative: number;
}

// ---------------------------------------------------------------------------
// Analytics (from REST /api/analytics/*)
// ---------------------------------------------------------------------------

export interface HealthMetric {
  name: string;
  value: number | null;
  status: string;    // healthy, warning, risk, pending
  threshold: string;
}

export interface HealthDashboard {
  metrics: HealthMetric[];
  overall_status: string;
  total_trades: number;
  note: string;
}

export interface CalibrationBin {
  predicted_low: number;
  predicted_high: number;
  actual_frequency: number;
  n_obs: number;
}

export interface CumulativePnlPoint {
  timestamp: number;
  cumulative_pnl: number;
  drawdown: number;
}

export interface TradingParams {
  K_frac: number;
  z: number;
  theta_entry: number;
  theta_exit: number;
  cooldown_seconds: number;
  low_confidence_multiplier: number;
  rapid_entry_enabled: boolean;
  bet365_divergence_auto_exit: boolean;
  f_order_cap: number;
  f_match_cap: number;
  f_total_cap: number;
  trading_mode: string;
}

export interface Alert {
  severity: string;
  title: string;
  body: string;
  match_id: string | null;
  timestamp: string;
}

// ---------------------------------------------------------------------------
// Event Log
// ---------------------------------------------------------------------------

export type EventType =
  | 'PRELIMINARY'
  | 'CONFIRMED'
  | 'VAR_CANCELLED'
  | 'OB_FREEZE'
  | 'COOLDOWN'
  | 'SIGNAL'
  | 'ORDER'
  | 'TICK';

export interface EventLogEntry {
  timestamp: number;
  type: EventType;
  message: string;
  data?: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// Active match list (from REST /api/matches/active)
// ---------------------------------------------------------------------------

export interface ActiveMatch {
  match_id: string;
  home: string;
  away: string;
  status: string;
}

// ---------------------------------------------------------------------------
// Price history point (accumulated from WebSocket for charting)
// ---------------------------------------------------------------------------

export interface PricePoint {
  t: number;
  P_true: number;
  P_kalshi_mid: number;
  P_kalshi_bid: number;
  P_kalshi_ask: number;
  P_bet365: number;
  event_state: string;
}
