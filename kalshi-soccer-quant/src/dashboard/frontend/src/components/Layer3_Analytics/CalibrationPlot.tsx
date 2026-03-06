import React, { useEffect, useState } from 'react';
import {
  ComposedChart,
  Bar,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ReferenceLine,
  ResponsiveContainer,
  ErrorBar,
} from 'recharts';
import type { CalibrationBin } from '../../types';

const API_BASE = process.env.REACT_APP_API_URL || 'http://localhost:8000';

const MARKET_TABS = [
  { key: 'all', label: 'All' },
  { key: '1x2', label: '1X2' },
  { key: 'over_under', label: 'Over/Under' },
  { key: 'btts', label: 'BTTS' },
] as const;

/**
 * 3B: Calibration Plot — reliability diagram.
 *
 * X-axis: predicted probability bins (0.0 to 1.0, 10 bins)
 * Y-axis: actual outcome frequency
 * Perfect calibration = diagonal line
 *
 * Shows 95% CI bands based on binomial distribution per bin.
 * Only shows bins with >= 20 observations.
 *
 * Tab selector: All / 1X2 / Over-Under / BTTS
 *
 * Data: REST /api/analytics/calibration polled every 60s.
 */
const CalibrationPlot: React.FC = () => {
  const [market, setMarket] = useState<string>('all');
  const [bins, setBins] = useState<CalibrationBin[]>([]);
  const [note, setNote] = useState<string>('');

  useEffect(() => {
    const fetchCalibration = async () => {
      try {
        const resp = await fetch(`${API_BASE}/api/analytics/calibration?market=${market}`);
        if (resp.ok) {
          const data = await resp.json();
          setBins(data.bins || []);
          setNote(data.note || '');
        }
      } catch {
        // Retry on next poll
      }
    };

    fetchCalibration();
    const interval = setInterval(fetchCalibration, 60000);
    return () => clearInterval(interval);
  }, [market]);

  // Filter bins with >= 20 observations and compute chart data
  const chartData = bins
    .filter((b) => b.n_obs >= 20)
    .map((b) => {
      const midpoint = (b.predicted_low + b.predicted_high) / 2;
      // 95% CI for binomial: p +/- 1.96 * sqrt(p*(1-p)/n)
      const p = b.actual_frequency;
      const n = b.n_obs;
      const se = Math.sqrt((p * (1 - p)) / n);
      const ci = 1.96 * se;
      return {
        bin: `${(b.predicted_low * 100).toFixed(0)}-${(b.predicted_high * 100).toFixed(0)}%`,
        midpoint,
        actual: b.actual_frequency,
        perfect: midpoint,
        ci_low: Math.max(0, p - ci),
        ci_high: Math.min(1, p + ci),
        error: ci,
        n_obs: b.n_obs,
      };
    });

  const hasData = chartData.length > 0;

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
        <span style={{ fontSize: '14px', fontWeight: 600, color: '#e2e8f0' }}>
          Calibration Plot
        </span>

        {/* Market tab selector */}
        <div style={{ display: 'flex', gap: '4px' }}>
          {MARKET_TABS.map((tab) => (
            <button
              key={tab.key}
              onClick={() => setMarket(tab.key)}
              style={{
                padding: '3px 10px',
                fontSize: '11px',
                fontWeight: market === tab.key ? 700 : 400,
                color: market === tab.key ? '#3b82f6' : '#94a3b8',
                backgroundColor: market === tab.key ? '#1e3a5f' : '#1e293b',
                border: '1px solid #334155',
                borderRadius: '4px',
                cursor: 'pointer',
              }}
            >
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      <div style={{
        backgroundColor: '#0f172a',
        borderRadius: '6px',
        border: '1px solid #334155',
        padding: '8px 4px 4px 0',
      }}>
        {!hasData ? (
          <div style={{ height: '260px', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <span style={{ color: '#64748b', fontSize: '13px' }}>
              {note || 'Insufficient data for calibration plot. Requires settled positions with >= 20 observations per bin.'}
            </span>
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={260}>
            <ComposedChart data={chartData} margin={{ top: 10, right: 15, left: 0, bottom: 5 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />

              <XAxis
                dataKey="bin"
                stroke="#475569"
                tick={{ fontSize: 10, fill: '#64748b' }}
                label={{ value: 'Predicted Probability', position: 'insideBottom', offset: -2, fontSize: 11, fill: '#64748b' }}
              />

              <YAxis
                domain={[0, 1]}
                stroke="#475569"
                tick={{ fontSize: 10, fill: '#64748b' }}
                tickFormatter={(v: number) => `${(v * 100).toFixed(0)}%`}
                width={45}
                label={{ value: 'Actual Frequency', angle: -90, position: 'insideLeft', offset: 10, fontSize: 11, fill: '#64748b' }}
              />

              <Tooltip
                contentStyle={{
                  backgroundColor: '#1e293b',
                  border: '1px solid #334155',
                  borderRadius: '6px',
                  fontSize: '11px',
                }}
                formatter={(value: number, name: string) => {
                  if (name === 'Actual') return [`${(value * 100).toFixed(1)}%`, name];
                  return [`${(value * 100).toFixed(1)}%`, name];
                }}
              />

              {/* Perfect calibration diagonal (rendered as a line through the data) */}
              <Line
                dataKey="perfect"
                type="linear"
                stroke="#475569"
                strokeDasharray="6 3"
                strokeWidth={1}
                dot={false}
                name="Perfect"
                isAnimationActive={false}
              />

              {/* Actual frequency bars with error bars for 95% CI */}
              <Bar
                dataKey="actual"
                fill="#3b82f6"
                fillOpacity={0.7}
                name="Actual"
                isAnimationActive={false}
              >
                <ErrorBar
                  dataKey="error"
                  width={4}
                  strokeWidth={1.5}
                  stroke="#94a3b8"
                />
              </Bar>
            </ComposedChart>
          </ResponsiveContainer>
        )}
      </div>

      {note && hasData && (
        <div style={{ fontSize: '11px', color: '#64748b', marginTop: '4px', fontStyle: 'italic' }}>
          {note}
        </div>
      )}
    </div>
  );
};

export default CalibrationPlot;
