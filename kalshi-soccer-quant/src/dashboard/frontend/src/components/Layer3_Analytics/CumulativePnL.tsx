import React, { useEffect, useState } from 'react';
import {
  ComposedChart,
  Area,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ReferenceLine,
  ResponsiveContainer,
  Legend,
} from 'recharts';
import type { CumulativePnlPoint } from '../../types';
import { formatPnl } from '../../utils/formatters';

const API_BASE = process.env.REACT_APP_API_URL || '';

interface ApiResponse {
  series: CumulativePnlPoint[];
  max_drawdown_pct: number;
}

/**
 * 3C: Cumulative P&L + Drawdown chart.
 *
 * Main line: cumulative realized P&L (blue solid)
 * Drawdown regions: red shaded area between P&L and running max
 * Reference: Phase 1.5 simulation P&L (green dashed, if available)
 *
 * X-axis: dates
 * Y-axis: cumulative $
 *
 * Data: REST /api/analytics/pnl_cumulative polled every 30s.
 */
const CumulativePnL: React.FC = () => {
  const [series, setSeries] = useState<CumulativePnlPoint[]>([]);
  const [maxDd, setMaxDd] = useState(0);

  useEffect(() => {
    const fetchData = async () => {
      try {
        const resp = await fetch(`${API_BASE}/api/analytics/pnl_cumulative`);
        if (resp.ok) {
          const data: ApiResponse = await resp.json();
          setSeries(data.series || []);
          setMaxDd(data.max_drawdown_pct || 0);
        }
      } catch {
        // Retry on next poll
      }
    };

    fetchData();
    const interval = setInterval(fetchData, 30000);
    return () => clearInterval(interval);
  }, []);

  // Build chart data: compute running max for drawdown shading
  const chartData = series.map((p) => ({
    time: p.timestamp,
    pnl: p.cumulative_pnl,
    drawdown: -p.drawdown, // negative for below-zero shading
    runningMax: p.cumulative_pnl + p.drawdown,
  }));

  const hasData = chartData.length > 0;

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
        <span style={{ fontSize: '14px', fontWeight: 600, color: '#e2e8f0' }}>
          Cumulative P&L
        </span>
        <span style={{ fontSize: '12px', color: maxDd > 20 ? '#ef4444' : maxDd > 10 ? '#eab308' : '#94a3b8' }}>
          Max Drawdown: {maxDd.toFixed(1)}%
        </span>
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
              No trade history yet. P&L chart will appear after trades settle.
            </span>
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={260}>
            <ComposedChart data={chartData} margin={{ top: 5, right: 15, left: 0, bottom: 5 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />

              <XAxis
                dataKey="time"
                type="number"
                domain={['dataMin', 'dataMax']}
                stroke="#475569"
                tick={{ fontSize: 10, fill: '#64748b' }}
                tickFormatter={formatDateLabel}
              />

              <YAxis
                stroke="#475569"
                tick={{ fontSize: 10, fill: '#64748b' }}
                tickFormatter={(v: number) => `$${v}`}
                width={55}
              />

              <Tooltip
                contentStyle={{
                  backgroundColor: '#1e293b',
                  border: '1px solid #334155',
                  borderRadius: '6px',
                  fontSize: '11px',
                }}
                labelFormatter={formatDateLabel}
                formatter={(value: number, name: string) => {
                  if (name === 'Drawdown') return [formatPnl(-value), name];
                  return [formatPnl(value), name];
                }}
              />

              <Legend wrapperStyle={{ fontSize: '11px', color: '#94a3b8' }} />

              {/* Zero line */}
              <ReferenceLine y={0} stroke="#475569" strokeDasharray="3 3" />

              {/* Drawdown shaded area (red, below the running max) */}
              <Area
                dataKey="drawdown"
                type="monotone"
                fill="#ef4444"
                fillOpacity={0.15}
                stroke="none"
                name="Drawdown"
                isAnimationActive={false}
                baseLine={0}
              />

              {/* Running max (faint gray dashed) */}
              <Line
                dataKey="runningMax"
                type="monotone"
                stroke="#475569"
                strokeDasharray="4 4"
                strokeWidth={1}
                dot={false}
                name="Peak"
                isAnimationActive={false}
              />

              {/* Cumulative P&L (primary blue line) */}
              <Line
                dataKey="pnl"
                type="monotone"
                stroke="#3b82f6"
                strokeWidth={2}
                dot={false}
                name="Cumulative P&L"
                isAnimationActive={false}
              />
            </ComposedChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
};

function formatDateLabel(ts: number): string {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

export default CumulativePnL;
