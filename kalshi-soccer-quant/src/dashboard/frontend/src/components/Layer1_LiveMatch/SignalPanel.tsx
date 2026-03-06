import React from 'react';
import type { MatchState, MarketSignal, MatchPosition } from '../../types';
import { formatCents, formatPnl } from '../../utils/formatters';

interface SignalPanelProps {
  state: MatchState | null;
}

const MARKETS = ['over_25', 'home_win', 'draw', 'away_win', 'btts'] as const;
const MARKET_LABELS: Record<string, string> = {
  over_25: 'Over 2.5',
  home_win: 'Home Win',
  draw: 'Draw',
  away_win: 'Away Win',
  btts: 'BTTS',
};

/**
 * 1D: Signal Panel — active signals and open positions for this match.
 *
 * Section 1: Active Signals — current tick's signal for each market
 *   Market | Direction | EV | Alignment | Suggested Qty
 *
 * Section 2: Open Positions — positions for this match
 *   Market | Dir | Entry | Current | P&L | bet365 aligned?
 */
const SignalPanel: React.FC<SignalPanelProps> = ({ state }) => {
  if (!state) return null;

  const signals = state.signals || [];
  const positions = state.positions || [];

  // Build signal lookup from per-market signals, or derive from single-market fields
  const signalRows: MarketSignal[] = signals.length > 0
    ? signals
    : deriveSingleSignal(state);

  return (
    <div style={{ marginTop: '8px' }}>
      {/* Section 1: Active Signals */}
      <div>
        <span style={{ fontSize: '13px', fontWeight: 600, color: '#94a3b8' }}>
          Active Signals
        </span>
        <div style={{
          backgroundColor: '#0f172a',
          border: '1px solid #334155',
          borderRadius: '6px',
          marginTop: '4px',
          overflow: 'hidden',
        }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12px' }}>
            <thead>
              <tr style={{ borderBottom: '1px solid #334155' }}>
                <Th>Market</Th>
                <Th>Direction</Th>
                <Th align="right">EV</Th>
                <Th>Alignment</Th>
                <Th align="right">Qty</Th>
              </tr>
            </thead>
            <tbody>
              {signalRows.length === 0 ? (
                <tr>
                  <td colSpan={5} style={{ padding: '12px', textAlign: 'center', color: '#64748b' }}>
                    No active signals
                  </td>
                </tr>
              ) : (
                signalRows.map((s) => (
                  <tr key={s.market} style={{ borderBottom: '1px solid #1e293b' }}>
                    <Td>{MARKET_LABELS[s.market] || s.market}</Td>
                    <Td>
                      <span style={{ color: directionColor(s.direction), fontWeight: 600 }}>
                        {s.direction}
                      </span>
                    </Td>
                    <Td align="right">
                      <span style={{ color: s.EV > 0 ? '#22c55e' : s.EV < 0 ? '#ef4444' : '#94a3b8' }}>
                        {(s.EV * 100).toFixed(1)}%
                      </span>
                    </Td>
                    <Td>
                      <AlignmentBadge alignment={s.alignment} />
                    </Td>
                    <Td align="right">{s.suggested_qty}</Td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Section 2: Open Positions */}
      <div style={{ marginTop: '8px' }}>
        <span style={{ fontSize: '13px', fontWeight: 600, color: '#94a3b8' }}>
          Open Positions ({positions.length})
        </span>
        <div style={{
          backgroundColor: '#0f172a',
          border: '1px solid #334155',
          borderRadius: '6px',
          marginTop: '4px',
          overflow: 'hidden',
        }}>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12px' }}>
            <thead>
              <tr style={{ borderBottom: '1px solid #334155' }}>
                <Th>Market</Th>
                <Th>Dir</Th>
                <Th align="right">Entry</Th>
                <Th align="right">Current</Th>
                <Th align="right">P&L</Th>
                <Th>bet365</Th>
              </tr>
            </thead>
            <tbody>
              {positions.length === 0 ? (
                <tr>
                  <td colSpan={6} style={{ padding: '12px', textAlign: 'center', color: '#64748b' }}>
                    No open positions
                  </td>
                </tr>
              ) : (
                positions.map((p, i) => (
                  <tr key={`${p.market}-${i}`} style={{ borderBottom: '1px solid #1e293b' }}>
                    <Td>{MARKET_LABELS[p.market] || p.market}</Td>
                    <Td>
                      <span style={{ color: directionColor(p.direction), fontWeight: 600 }}>
                        {p.direction}
                      </span>
                    </Td>
                    <Td align="right">{formatCents(p.entry_price)}</Td>
                    <Td align="right">{formatCents(p.current_price)}</Td>
                    <Td align="right">
                      <span style={{ color: p.pnl >= 0 ? '#22c55e' : '#ef4444', fontWeight: 600 }}>
                        {formatPnl(p.pnl)}
                      </span>
                    </Td>
                    <Td>
                      {p.bet365_aligned ? (
                        <span style={{ color: '#22c55e' }}>OK</span>
                      ) : (
                        <span style={{ color: '#eab308', fontWeight: 700 }}>WARN</span>
                      )}
                    </Td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
};

/** Derive a single signal row from legacy MatchState fields. */
function deriveSingleSignal(state: MatchState): MarketSignal[] {
  if (!state.EV || !state.direction || state.direction === 'HOLD') return [];
  return [{
    market: 'over_25',
    direction: state.direction,
    EV: state.EV,
    alignment: state.bet365_confidence || 'unknown',
    suggested_qty: 0,
  }];
}

function directionColor(dir: string): string {
  if (dir === 'BUY_YES') return '#22c55e';
  if (dir === 'BUY_NO') return '#ef4444';
  return '#94a3b8';
}

/** Table header cell. */
const Th: React.FC<{ children: React.ReactNode; align?: 'left' | 'right' }> = ({ children, align = 'left' }) => (
  <th style={{
    padding: '6px 10px',
    textAlign: align,
    color: '#64748b',
    fontWeight: 600,
    fontSize: '11px',
    textTransform: 'uppercase',
    letterSpacing: '0.05em',
  }}>
    {children}
  </th>
);

/** Table data cell. */
const Td: React.FC<{ children: React.ReactNode; align?: 'left' | 'right' }> = ({ children, align = 'left' }) => (
  <td style={{
    padding: '5px 10px',
    textAlign: align,
    color: '#cbd5e1',
  }}>
    {children}
  </td>
);

/** Alignment indicator badge. */
const AlignmentBadge: React.FC<{ alignment: string }> = ({ alignment }) => {
  const isAligned = alignment === 'aligned' || alignment === 'high';
  const isDivergent = alignment === 'divergent' || alignment === 'low';
  const color = isAligned ? '#22c55e' : isDivergent ? '#eab308' : '#64748b';
  const label = isAligned ? 'OK' : isDivergent ? 'WARN' : alignment;

  return (
    <span style={{ color, fontWeight: isAligned || isDivergent ? 600 : 400, fontSize: '11px' }}>
      {label}
    </span>
  );
};

export default SignalPanel;
