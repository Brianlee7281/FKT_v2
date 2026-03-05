import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  ComposedChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ReferenceLine,
  ResponsiveContainer,
  Legend,
} from 'recharts';
import type { MatchState } from '../../types';
import { CHART_COLORS } from '../../utils/colors';
import { formatMinute } from '../../utils/formatters';

/** Accumulated mu data point. */
interface MuPoint {
  t: number;
  mu_H: number;
  mu_A: number;
}

/** Jump marker when goals or red cards cause step changes in mu. */
interface MuJump {
  t: number;
  type: 'goal' | 'red_card';
}

const MU_JUMP_THRESHOLD = 0.003;

interface MuChartProps {
  state: MatchState | null;
}

/**
 * 1C: Mu Chart — shows mu_H and mu_A decay over time.
 *
 * Answers "Is the model processing events correctly?"
 * If mu doesn't jump after a goal, something is broken.
 *
 * Visual:
 *   - mu_H: blue line (home scoring rate)
 *   - mu_A: red line (away scoring rate)
 *   - Jump markers: vertical lines at goals/red cards
 */
const MuChart: React.FC<MuChartProps> = ({ state }) => {
  const [history, setHistory] = useState<MuPoint[]>([]);
  const [jumps, setJumps] = useState<MuJump[]>([]);
  const prevMu = useRef<{ mu_H?: number; mu_A?: number; t?: number }>({});

  // Accumulate mu history from state snapshots
  useEffect(() => {
    if (!state || state.t === undefined || state.mu_H === undefined || state.mu_A === undefined) return;

    const point: MuPoint = {
      t: state.t,
      mu_H: state.mu_H,
      mu_A: state.mu_A,
    };

    setHistory((prev) => {
      // Deduplicate same-minute
      if (prev.length > 0 && Math.abs(prev[prev.length - 1].t - point.t) < 0.01) {
        return [...prev.slice(0, -1), point];
      }
      return [...prev, point];
    });

    // Detect jumps: significant change in mu indicates goal or red card
    const prev = prevMu.current;
    if (prev.mu_H !== undefined && prev.mu_A !== undefined) {
      const dH = Math.abs(state.mu_H - prev.mu_H);
      const dA = Math.abs(state.mu_A - prev.mu_A);

      if (dH > MU_JUMP_THRESHOLD || dA > MU_JUMP_THRESHOLD) {
        // If both decreased, likely a red card; if one jumped up, likely a goal
        const type: MuJump['type'] =
          (state.mu_H < prev.mu_H && state.mu_A < prev.mu_A) ? 'red_card' : 'goal';
        setJumps((j) => [...j, { t: state.t!, type }]);
      }
    }

    prevMu.current = { mu_H: state.mu_H, mu_A: state.mu_A, t: state.t };
  }, [state]);

  const chartData = useMemo(() => history, [history]);
  const hasData = chartData.length > 0;

  return (
    <div style={{ marginTop: '8px' }}>
      <span style={{ fontSize: '13px', fontWeight: 600, color: '#94a3b8' }}>
        Scoring Rates (mu)
      </span>

      <div
        style={{
          backgroundColor: '#0f172a',
          borderRadius: '6px',
          border: '1px solid #334155',
          padding: '8px 4px 4px 0',
          marginTop: '4px',
        }}
      >
        {!hasData ? (
          <div style={{ height: '160px', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <span style={{ color: '#64748b', fontSize: '13px' }}>
              Waiting for mu data...
            </span>
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={160}>
            <ComposedChart data={chartData} margin={{ top: 5, right: 10, left: 0, bottom: 5 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />

              <XAxis
                dataKey="t"
                type="number"
                domain={['dataMin', 'dataMax']}
                tickFormatter={(t: number) => `${Math.floor(t)}'`}
                stroke="#475569"
                tick={{ fontSize: 10, fill: '#64748b' }}
              />

              <YAxis
                tickFormatter={(v: number) => v.toFixed(3)}
                stroke="#475569"
                tick={{ fontSize: 10, fill: '#64748b' }}
                width={50}
              />

              <Tooltip
                contentStyle={{
                  backgroundColor: '#1e293b',
                  border: '1px solid #334155',
                  borderRadius: '6px',
                  fontSize: '11px',
                }}
                labelFormatter={(t: number) => formatMinute(t)}
                formatter={(value: number, name: string) => [value.toFixed(4), name]}
              />

              <Legend wrapperStyle={{ fontSize: '10px', color: '#94a3b8' }} />

              {/* mu_H: blue (home scoring rate) */}
              <Line
                dataKey="mu_H"
                type="monotone"
                stroke={CHART_COLORS.P_true}
                strokeWidth={2}
                dot={false}
                name="mu_H (Home)"
                isAnimationActive={false}
              />

              {/* mu_A: red (away scoring rate) */}
              <Line
                dataKey="mu_A"
                type="monotone"
                stroke={CHART_COLORS.P_kalshi}
                strokeWidth={2}
                dot={false}
                name="mu_A (Away)"
                isAnimationActive={false}
              />

              {/* Jump markers */}
              {jumps.map((j, i) => (
                <ReferenceLine
                  key={`jump-${i}`}
                  x={j.t}
                  stroke={j.type === 'goal' ? CHART_COLORS.goal_confirmed : CHART_COLORS.red_card}
                  strokeDasharray="4 4"
                  strokeWidth={1.5}
                  label={{
                    value: j.type === 'goal' ? 'GOAL' : 'RED',
                    fill: j.type === 'goal' ? CHART_COLORS.goal_confirmed : CHART_COLORS.red_card,
                    fontSize: 9,
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

export default MuChart;
