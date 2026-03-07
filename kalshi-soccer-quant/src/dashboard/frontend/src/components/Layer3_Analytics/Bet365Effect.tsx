import React, { useEffect, useState } from 'react';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Cell,
} from 'recharts';

const API_BASE = process.env.REACT_APP_API_URL || '';

interface GroupStats {
  avg_return: number;
  n_trades: number;
  win_rate: number;
}

interface ApiResponse {
  aligned: GroupStats;
  divergent: GroupStats;
  alignment_value: number;
}

/**
 * 3E: Bet365 Alignment Effect — cross-validation value analysis.
 *
 * Bar chart: ALIGNED vs DIVERGENT avg return per trade
 * Alignment Value: difference in cents
 * Win rate comparison with delta
 *
 * Data: REST /api/analytics/alignment_effect polled every 30s.
 */
const Bet365Effect: React.FC = () => {
  const [data, setData] = useState<ApiResponse | null>(null);

  useEffect(() => {
    const fetchData = async () => {
      try {
        const resp = await fetch(`${API_BASE}/api/analytics/alignment_effect`);
        if (resp.ok) setData(await resp.json());
      } catch {
        // Retry on next poll
      }
    };

    fetchData();
    const interval = setInterval(fetchData, 30000);
    return () => clearInterval(interval);
  }, []);

  if (!data) {
    return (
      <div style={{ textAlign: 'center', padding: '40px', color: '#64748b' }}>
        Loading alignment analysis...
      </div>
    );
  }

  const hasData = data.aligned.n_trades > 0 || data.divergent.n_trades > 0;

  const chartBars = [
    { group: 'ALIGNED', avg_return: data.aligned.avg_return, n: data.aligned.n_trades, color: '#22c55e' },
    { group: 'DIVERGENT', avg_return: data.divergent.avg_return, n: data.divergent.n_trades, color: '#eab308' },
  ];

  const av = data.alignment_value;
  const avColor = av > 0.01 ? '#22c55e' : av < -0.01 ? '#ef4444' : '#94a3b8';
  const avMessage = av > 0.01
    ? 'Market alignment check is adding value.'
    : av < -0.01
    ? 'Market alignment check is not adding value. Review divergence logic.'
    : 'Insufficient difference to determine alignment value.';

  const winDelta = (data.aligned.win_rate - data.divergent.win_rate) * 100;

  return (
    <div>
      <span style={{ fontSize: '14px', fontWeight: 600, color: '#e2e8f0' }}>
        bet365 Alignment Effect
      </span>

      {!hasData ? (
        <div style={{
          backgroundColor: '#0f172a',
          borderRadius: '6px',
          border: '1px solid #334155',
          padding: '40px',
          marginTop: '8px',
          textAlign: 'center',
          color: '#64748b',
          fontSize: '13px',
        }}>
          No alignment data yet. Requires trades with bet365 cross-validation.
        </div>
      ) : (
        <>
          {/* Bar chart: avg return per group */}
          <div style={{
            backgroundColor: '#0f172a',
            borderRadius: '6px',
            border: '1px solid #334155',
            padding: '8px 4px 4px 0',
            marginTop: '8px',
          }}>
            <ResponsiveContainer width="100%" height={180}>
              <BarChart data={chartBars} margin={{ top: 10, right: 15, left: 0, bottom: 5 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
                <XAxis
                  dataKey="group"
                  stroke="#475569"
                  tick={{ fontSize: 11, fill: '#94a3b8' }}
                />
                <YAxis
                  stroke="#475569"
                  tick={{ fontSize: 10, fill: '#64748b' }}
                  tickFormatter={(v: number) => `${(v * 100).toFixed(1)}c`}
                  width={50}
                />
                <Tooltip
                  contentStyle={{
                    backgroundColor: '#1e293b',
                    border: '1px solid #334155',
                    borderRadius: '6px',
                    fontSize: '11px',
                  }}
                  formatter={(value: number) => [
                    `${(value * 100).toFixed(1)}c/trade`,
                    'Avg Return',
                  ]}
                />
                <Bar dataKey="avg_return" isAnimationActive={false} radius={[4, 4, 0, 0]}>
                  {chartBars.map((entry, i) => (
                    <Cell key={i} fill={entry.color} fillOpacity={0.8} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>

          {/* Alignment Value + Win Rate comparison */}
          <div style={{
            backgroundColor: '#1e293b',
            borderRadius: '6px',
            padding: '12px 14px',
            marginTop: '8px',
          }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
              <span style={{ fontSize: '12px', color: '#94a3b8' }}>Alignment Value</span>
              <span style={{ fontSize: '16px', fontWeight: 700, color: avColor }}>
                {av >= 0 ? '+' : ''}{(av * 100).toFixed(1)}c
              </span>
            </div>
            <div style={{ fontSize: '11px', color: '#64748b', marginBottom: '10px' }}>
              {avMessage}
            </div>

            {/* Win rate comparison */}
            <div style={{ borderTop: '1px solid #334155', paddingTop: '8px' }}>
              <div style={{ fontSize: '11px', color: '#94a3b8', marginBottom: '6px', fontWeight: 600 }}>
                Win Rate Comparison
              </div>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '12px', marginBottom: '2px' }}>
                <span style={{ color: '#22c55e' }}>ALIGNED: {(data.aligned.win_rate * 100).toFixed(1)}%</span>
                <span style={{ color: '#eab308' }}>DIVERGENT: {(data.divergent.win_rate * 100).toFixed(1)}%</span>
                <span style={{ color: '#e2e8f0', fontWeight: 600 }}>
                  {'\u0394'}: {winDelta >= 0 ? '+' : ''}{winDelta.toFixed(1)}pp
                </span>
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
};

export default Bet365Effect;
