/** State-to-color mapping for dashboard components. */

// Engine state -> header border/background color
export const STATE_COLORS: Record<string, { border: string; bg: string }> = {
  IDLE:             { border: '#22c55e', bg: 'transparent' },  // green
  PRELIMINARY:      { border: '#eab308', bg: '#fef3c7' },      // yellow bg
  CONFIRMED:        { border: '#22c55e', bg: '#d1fae5' },      // green bg (brief flash)
  VAR_CANCELLED:    { border: '#ef4444', bg: '#fee2e2' },      // red
  COOLDOWN:         { border: '#3b82f6', bg: 'transparent' },  // blue
  OB_FREEZE:        { border: '#ef4444', bg: 'transparent' },  // red
  HALFTIME:         { border: '#6b7280', bg: '#374151' },      // gray
  FINISHED:         { border: '#374151', bg: '#1e293b' },      // dark
};

// Event log type -> color
export const EVENT_COLORS: Record<string, { bg: string; text: string }> = {
  PRELIMINARY:    { bg: '#fef3c7', text: '#92400e' },  // yellow
  CONFIRMED:      { bg: '#d1fae5', text: '#065f46' },  // green
  VAR_CANCELLED:  { bg: 'transparent', text: '#dc2626' },  // red text
  OB_FREEZE:      { bg: '#fee2e2', text: '#991b1b' },  // red bg
  COOLDOWN:       { bg: '#dbeafe', text: '#1e40af' },  // blue
  SIGNAL:         { bg: 'transparent', text: '#a855f7' },  // purple
  ORDER:          { bg: 'transparent', text: '#e2e8f0' },  // bold white
  TICK:           { bg: 'transparent', text: '#6b7280' },  // gray
};

// Chart line colors
export const CHART_COLORS = {
  P_true: '#3b82f6',        // blue
  P_kalshi: '#ef4444',      // red
  P_kalshi_band: '#fca5a5', // light red (bid-ask spread)
  P_bet365: '#22c55e',      // green
  edge_zone: '#93c5fd',     // light blue
  goal_confirmed: '#22c55e',
  goal_preliminary: '#eab308',
  goal_var_cancelled: '#ef4444',
  red_card: '#dc2626',
  halftime: '#374151',
  trade_entry: '#22c55e',
  trade_exit: '#ef4444',
};

// Alert severity -> color
export const ALERT_COLORS: Record<string, string> = {
  info: '#3b82f6',
  success: '#22c55e',
  warning: '#eab308',
  critical: '#ef4444',
};

// Trading mode badge
export const MODE_COLORS: Record<string, { bg: string; text: string }> = {
  paper: { bg: '#7c3aed', text: '#ffffff' },  // purple
  live:  { bg: '#22c55e', text: '#ffffff' },   // green
};
