import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  ComposedChart,
  Line,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ReferenceLine,
  ReferenceArea,
  ResponsiveContainer,
  Legend,
} from 'recharts';
import type { MatchState, PricePoint } from '../../types';
import { CHART_COLORS } from '../../utils/colors';
import { formatMinute, formatProb } from '../../utils/formatters';

const MARKETS = ['over_25', 'home_win', 'draw', 'away_win', 'btts'] as const;
const MARKET_LABELS: Record<string, string> = {
  over_25: 'Over 2.5',
  home_win: 'Home Win',
  draw: 'Draw',
  away_win: 'Away Win',
  btts: 'BTTS',
};

interface PriceChartProps {
  state: MatchState | null;
}

/** Track event markers (goals, red cards) accumulated over time. */
interface EventMarker {
  t: number;
  type: 'PRELIMINARY' | 'CONFIRMED' | 'VAR_CANCELLED' | 'RED_CARD';
}

/**
 * 1B: Model vs Market Comparison Chart — the core visualization.
 *
 * Accumulates price history from WebSocket state snapshots and renders:
 *   - P_true (blue solid, thick)
 *   - P_kalshi mid (red solid) with bid-ask band (red shaded)
 *   - P_bet365 (green dashed)
 *   - Edge zones (light blue where P_true > P_kalshi_ask)
 *   - Event markers (goals, red cards, halftime)
 */
const PriceChart: React.FC<PriceChartProps> = ({ state }) => {
  const [selectedMarket, setSelectedMarket] = useState<string>('over_25');
  const [history, setHistory] = useState<PricePoint[]>([]);
  const [markers, setMarkers] = useState<EventMarker[]>([]);
  const prevEventState = useRef<string>('IDLE');

  // Accumulate price history from state snapshots
  useEffect(() => {
    if (!state || state.t === undefined) return;

    const pTrue = state.P_true?.[selectedMarket] ?? null;
    const pBet365 = state.P_bet365?.[selectedMarket] ?? null;
    const pBid = state.P_kalshi_bid ?? null;
    const pAsk = state.P_kalshi_ask ?? null;
    const pMid = pBid !== null && pAsk !== null ? (pBid + pAsk) / 2 : null;

    // Skip if no pricing data at all
    if (pTrue === null && pMid === null && pBet365 === null) return;

    const point: PricePoint = {
      t: state.t,
      P_true: pTrue ?? 0,
      P_kalshi_mid: pMid ?? 0,
      P_kalshi_bid: pBid ?? 0,
      P_kalshi_ask: pAsk ?? 0,
      P_bet365: pBet365 ?? 0,
      event_state: state.event_state || 'IDLE',
    };

    setHistory((prev) => {
      // Deduplicate: skip if same minute (within 0.01)
      if (prev.length > 0 && Math.abs(prev[prev.length - 1].t - point.t) < 0.01) {
        return [...prev.slice(0, -1), point];
      }
      return [...prev, point];
    });

    // Detect event transitions for markers
    const currentEvent = state.event_state || 'IDLE';
    if (currentEvent !== prevEventState.current) {
      if (currentEvent === 'PRELIMINARY' || currentEvent === 'CONFIRMED' || currentEvent === 'VAR_CANCELLED') {
        setMarkers((prev) => [...prev, { t: state.t!, type: currentEvent as EventMarker['type'] }]);
      }
      prevEventState.current = currentEvent;
    }
  }, [state, selectedMarket]);

  // Reset history when market changes
  useEffect(() => {
    setHistory([]);
    setMarkers([]);
  }, [selectedMarket]);

  // Compute halftime regions from history
  const halftimeRegions = useMemo(() => {
    const regions: { start: number; end: number }[] = [];
    let htStart: number | null = null;

    for (const point of history) {
      const isHT = point.event_state === 'HALFTIME';
      if (isHT && htStart === null) {
        htStart = point.t;
      } else if (!isHT && htStart !== null) {
        regions.push({ start: htStart, end: point.t });
        htStart = null;
      }
    }
    if (htStart !== null && history.length > 0) {
      regions.push({ start: htStart, end: history[history.length - 1].t });
    }
    return regions;
  }, [history]);

  // Chart data with edge zone computation
  const chartData = useMemo(
    () =>
      history.map((p) => ({
        ...p,
        // For the bid-ask band Area
        bidAsk: [p.P_kalshi_bid, p.P_kalshi_ask] as [number, number],
        // Edge zone: shade where P_true > P_kalshi_ask
        edge: p.P_true > p.P_kalshi_ask ? p.P_true : undefined,
        edgeBase: p.P_true > p.P_kalshi_ask ? p.P_kalshi_ask : undefined,
      })),
    [history]
  );

  const hasData = chartData.length > 0;

  return (
    <div style={{ marginTop: '8px' }}>
      {/* Market tab selector */}
      <div style={{ display: 'flex', gap: '0', marginBottom: '8px' }}>
        {MARKETS.map((m) => (
          <button
            key={m}
            onClick={() => setSelectedMarket(m)}
            style={{
              padding: '4px 12px',
              fontSize: '12px',
              fontWeight: selectedMarket === m ? 700 : 400,
              color: selectedMarket === m ? '#3b82f6' : '#94a3b8',
              backgroundColor: selectedMarket === m ? '#1e3a5f' : '#1e293b',
              border: '1px solid #334155',
              borderBottom: selectedMarket === m ? '2px solid #3b82f6' : '1px solid #334155',
              cursor: 'pointer',
              outline: 'none',
            }}
          >
            {MARKET_LABELS[m] || m}
          </button>
        ))}
      </div>

      {/* Chart */}
      <div
        style={{
          backgroundColor: '#0f172a',
          borderRadius: '6px',
          border: '1px solid #334155',
          padding: '8px 4px 4px 0',
        }}
      >
        {!hasData ? (
          <div style={{ height: '300px', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <span style={{ color: '#64748b', fontSize: '14px' }}>
              Waiting for price data...
            </span>
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={300}>
            <ComposedChart data={chartData} margin={{ top: 5, right: 10, left: 0, bottom: 5 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />

              <XAxis
                dataKey="t"
                type="number"
                domain={['dataMin', 'dataMax']}
                tickFormatter={(t: number) => `${Math.floor(t)}'`}
                stroke="#475569"
                tick={{ fontSize: 11, fill: '#64748b' }}
              />

              <YAxis
                domain={[0, 1]}
                tickFormatter={(v: number) => `${(v * 100).toFixed(0)}%`}
                stroke="#475569"
                tick={{ fontSize: 11, fill: '#64748b' }}
                width={45}
              />

              <Tooltip
                contentStyle={{
                  backgroundColor: '#1e293b',
                  border: '1px solid #334155',
                  borderRadius: '6px',
                  fontSize: '12px',
                }}
                labelFormatter={(t: number) => formatMinute(t)}
                formatter={(value: number, name: string) => [formatProb(value), name]}
              />

              <Legend
                wrapperStyle={{ fontSize: '11px', color: '#94a3b8' }}
              />

              {/* Halftime gray shaded regions */}
              {halftimeRegions.map((r, i) => (
                <ReferenceArea
                  key={`ht-${i}`}
                  x1={r.start}
                  x2={r.end}
                  fill={CHART_COLORS.halftime}
                  fillOpacity={0.4}
                  label={{ value: 'HT', fill: '#64748b', fontSize: 10 }}
                />
              ))}

              {/* Kalshi bid-ask spread band */}
              <Area
                dataKey="bidAsk"
                type="monotone"
                stroke="none"
                fill={CHART_COLORS.P_kalshi_band}
                fillOpacity={0.3}
                name="Bid-Ask Spread"
                legendType="none"
                isAnimationActive={false}
              />

              {/* P_true — blue solid thick */}
              <Line
                dataKey="P_true"
                type="monotone"
                stroke={CHART_COLORS.P_true}
                strokeWidth={2.5}
                dot={false}
                name="P_true (Model)"
                isAnimationActive={false}
              />

              {/* P_kalshi mid — red solid */}
              <Line
                dataKey="P_kalshi_mid"
                type="monotone"
                stroke={CHART_COLORS.P_kalshi}
                strokeWidth={1.5}
                dot={false}
                name="P_kalshi (Market)"
                isAnimationActive={false}
              />

              {/* P_bet365 — green dashed */}
              <Line
                dataKey="P_bet365"
                type="monotone"
                stroke={CHART_COLORS.P_bet365}
                strokeWidth={1.5}
                strokeDasharray="6 3"
                dot={false}
                name="P_bet365"
                isAnimationActive={false}
              />

              {/* Event markers: goal / red card vertical lines */}
              {markers.map((m, i) => (
                <ReferenceLine
                  key={`ev-${i}`}
                  x={m.t}
                  stroke={markerColor(m.type)}
                  strokeDasharray={m.type === 'PRELIMINARY' ? '4 4' : undefined}
                  strokeWidth={m.type === 'CONFIRMED' ? 2 : 1.5}
                  label={{
                    value: markerLabel(m.type),
                    fill: markerColor(m.type),
                    fontSize: 11,
                    position: 'top',
                  }}
                />
              ))}
            </ComposedChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
};

function markerColor(type: EventMarker['type']): string {
  switch (type) {
    case 'PRELIMINARY': return CHART_COLORS.goal_preliminary;
    case 'CONFIRMED': return CHART_COLORS.goal_confirmed;
    case 'VAR_CANCELLED': return CHART_COLORS.goal_var_cancelled;
    case 'RED_CARD': return CHART_COLORS.red_card;
  }
}

function markerLabel(type: EventMarker['type']): string {
  switch (type) {
    case 'PRELIMINARY': return 'PRELIM';
    case 'CONFIRMED': return 'GOAL';
    case 'VAR_CANCELLED': return 'VAR X';
    case 'RED_CARD': return 'RED';
  }
}

export default PriceChart;
