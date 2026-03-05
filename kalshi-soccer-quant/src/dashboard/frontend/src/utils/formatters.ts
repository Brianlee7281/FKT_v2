/** Formatting utilities for dashboard display values. */

/** Format match minute (decimal) as "mm:ss". */
export function formatMinute(t: number | undefined): string {
  if (t === undefined || t === null) return '--:--';
  const minutes = Math.floor(t);
  const seconds = Math.floor((t - minutes) * 60);
  return `${minutes}:${seconds.toString().padStart(2, '0')}`;
}

/** Format probability as percentage string. */
export function formatProb(p: number | undefined | null): string {
  if (p === undefined || p === null) return '--';
  return `${(p * 100).toFixed(1)}%`;
}

/** Format price in cents (e.g., 0.55 -> "55c"). */
export function formatCents(p: number | undefined | null): string {
  if (p === undefined || p === null) return '--';
  return `${Math.round(p * 100)}c`;
}

/** Format P&L with sign and dollar symbol. */
export function formatPnl(pnl: number | undefined | null): string {
  if (pnl === undefined || pnl === null) return '--';
  const sign = pnl >= 0 ? '+' : '';
  return `${sign}$${pnl.toFixed(2)}`;
}

/** Format percentage with sign. */
export function formatPct(pct: number | undefined | null): string {
  if (pct === undefined || pct === null) return '--';
  const sign = pct >= 0 ? '+' : '';
  return `${sign}${pct.toFixed(1)}%`;
}

/** Format X state (player count) as "NvN". */
export function formatPlayerCount(X: number | undefined): string {
  if (X === undefined || X === null) return '--';
  // X encoding: 0=11v11, 1=10v11, 2=11v10, 3=10v10
  const mapping: Record<number, string> = {
    0: '11v11',
    1: '10v11',
    2: '11v10',
    3: '10v10',
  };
  return mapping[X] ?? `X=${X}`;
}

/** Format ISO timestamp to HH:MM:SS. */
export function formatTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString('en-US', { hour12: false });
  } catch {
    return iso;
  }
}

/** Format Unix timestamp to HH:MM:SS. */
export function formatUnixTime(ts: number): string {
  if (!ts) return '--';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('en-US', { hour12: false });
}
