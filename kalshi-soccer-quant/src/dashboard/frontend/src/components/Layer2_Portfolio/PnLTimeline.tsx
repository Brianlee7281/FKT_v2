import React, { useEffect, useState } from 'react';
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
import type { PnlPoint } from '../../types';
import { formatPnl } from '../../utils/formatters';

const API_BASE = process.env.REACT_APP_API_URL || 'http://localhost:8000';

/**
 * 2C: P&L Timeline — realized and total P&L over time.
 *
 * Two lines:
 *   Realized P&L (green solid, step function — jumps on settlement)
 *   Total P&L (blue solid, continuous — realized + unrealized)
 *
 * X-axis: time of day (UTC)
 * Y-axis: dollar P&L
 *
 * Data: REST /api/portfolio/pnl_timeline polled every 10s.
 */
const PnLTimeline: React.FC = () => {
  const [data, setData] = useState<PnlPoint[]>([]);

  useEffect(() => {
    const fetchPnl = async () => {
      try {
        const resp = await fetch(`${API_BASE}/api/portfolio/pnl_timeline`);
        if (resp.ok) setData(await resp.json());
      } catch {
        // Retry on next poll
      }
    };

    fetchPnl();
    const interval = setInterval(fetchPnl, 10000);
    return () => clearInterval(interval);
  }, []);

  // Build chart data: accumulate realized (step) and total (continuous)
  const chartData = buildChartData(data);
  const hasData = chartData.length > 0;

  return (
    <div>
      <span style={{ fontSize: '14px', fontWeight: 600, color: '#e2e8f0' }}>
        P&L Timeline
      </span>

      <div style={{
        backgroundColor: '#0f172a',
        borderRadius: '6px',
        border: '1px solid #334155',
        padding: '8px 4px 4px 0',
        marginTop: '8px',
      }}>
        {!hasData ? (
          <div style={{ height: '220px', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <span style={{ color: '#64748b', fontSize: '13px' }}>
              No P&L data yet. Trades will appear here as they settle.
            </span>
          </div>
        ) : (
          <ResponsiveContainer width="100%" height={220}>
            <ComposedChart data={chartData} margin={{ top: 5, right: 10, left: 0, bottom: 5 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />

              <XAxis
                dataKey="time"
                stroke="#475569"
                tick={{ fontSize: 10, fill: '#64748b' }}
                tickFormatter={formatTimeLabel}
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
                labelFormatter={formatTimeLabel}
                formatter={(value: number, name: string) => [formatPnl(value), name]}
              />

              <Legend wrapperStyle={{ fontSize: '11px', color: '#94a3b8' }} />

              {/* Zero line */}
              <ReferenceLine y={0} stroke="#475569" strokeDasharray="3 3" />

              {/* Realized P&L — step function (jumps on settlement) */}
              <Line
                dataKey="realized"
                type="stepAfter"
                stroke="#22c55e"
                strokeWidth={2}
                dot={false}
                name="Realized P&L"
                isAnimationActive={false}
              />

              {/* Total P&L — continuous (realized + unrealized) */}
              <Line
                dataKey="total"
                type="monotone"
                stroke="#3b82f6"
                strokeWidth={1.5}
                dot={false}
                name="Total P&L"
                isAnimationActive={false}
              />
            </ComposedChart>
          </ResponsiveContainer>
        )}
      </div>
    </div>
  );
};

interface ChartPoint {
  time: number;
  realized: number;
  total: number;
}

function buildChartData(points: PnlPoint[]): ChartPoint[] {
  if (points.length === 0) return [];

  // Points come sorted by timestamp with cumulative field
  // realized = cumulative (settled trades only)
  // total = cumulative (includes unrealized from the endpoint)
  return points.map((p) => ({
    time: p.timestamp,
    realized: p.cumulative,
    total: p.cumulative + (p.pnl - (p.pnl)),  // For now, realized = total until unrealized data is added
  }));
}

function formatTimeLabel(ts: number): string {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
}

export default PnLTimeline;
