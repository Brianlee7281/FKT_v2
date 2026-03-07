import React, { useEffect, useState } from 'react';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  Cell,
} from 'recharts';

const API_BASE = process.env.REACT_APP_API_URL || '';

interface ApiResponse {
  total_events: number;
  confirmed: number;
  var_cancelled: number;
  false_alarm: number;
  accuracy: number;
  rapid_entry_ready: boolean;
  rapid_entry_checks: {
    accuracy_gt_095: boolean;
    var_rate_lt_003: boolean;
    hypothetical_pnl_gt_0: boolean;
    trades_gte_200: boolean;
  };
}

/**
 * 3F: Preliminary Accuracy — PRELIMINARY detection accuracy + Rapid Entry readiness.
 *
 * Stacked bar: Confirmed Match / VAR Cancelled / False Alarm
 *
 * Rapid Entry Readiness checklist:
 *   Accuracy > 0.95
 *   VAR rate < 0.03
 *   Hypothetical P&L > 0
 *   Trades >= 200
 *
 * Data: REST /api/analytics/preliminary polled every 30s.
 */
const PrelimAccuracy: React.FC = () => {
  const [data, setData] = useState<ApiResponse | null>(null);

  useEffect(() => {
    const fetchData = async () => {
      try {
        const resp = await fetch(`${API_BASE}/api/analytics/preliminary`);
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
        Loading preliminary accuracy...
      </div>
    );
  }

  const hasData = data.total_events > 0;
  const confirmedPct = hasData ? (data.confirmed / data.total_events * 100).toFixed(1) : '0';
  const varPct = hasData ? (data.var_cancelled / data.total_events * 100).toFixed(1) : '0';
  const falsePct = hasData ? (data.false_alarm / data.total_events * 100).toFixed(1) : '0';

  const barData = [
    { name: 'Confirmed', value: data.confirmed, color: '#22c55e' },
    { name: 'VAR Cancelled', value: data.var_cancelled, color: '#eab308' },
    { name: 'False Alarm', value: data.false_alarm, color: '#ef4444' },
  ];

  const checks = data.rapid_entry_checks;
  const checkItems = [
    { label: `Accuracy > 0.95 (${data.accuracy.toFixed(3)})`, pass: checks.accuracy_gt_095 },
    { label: `VAR rate < 0.03 (${hasData ? (data.var_cancelled / data.total_events).toFixed(3) : '0'})`, pass: checks.var_rate_lt_003 },
    { label: 'Hypothetical P&L > 0', pass: checks.hypothetical_pnl_gt_0 },
    { label: `Trades >= 200 (${data.total_events})`, pass: checks.trades_gte_200 },
  ];

  const allPassed = checkItems.every((c) => c.pass);

  return (
    <div>
      <span style={{ fontSize: '14px', fontWeight: 600, color: '#e2e8f0' }}>
        Preliminary Accuracy
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
          No preliminary events recorded yet.
        </div>
      ) : (
        <>
          {/* Summary line */}
          <div style={{ fontSize: '12px', color: '#94a3b8', marginTop: '4px', marginBottom: '8px' }}>
            Total Preliminary Events: {data.total_events} —
            Confirmed: {data.confirmed} ({confirmedPct}%) |
            VAR: {data.var_cancelled} ({varPct}%) |
            False: {data.false_alarm} ({falsePct}%)
          </div>

          {/* Breakdown bar chart */}
          <div style={{
            backgroundColor: '#0f172a',
            borderRadius: '6px',
            border: '1px solid #334155',
            padding: '8px 4px 4px 0',
          }}>
            <ResponsiveContainer width="100%" height={120}>
              <BarChart data={barData} layout="vertical" margin={{ top: 5, right: 15, left: 5, bottom: 5 }}>
                <XAxis type="number" stroke="#475569" tick={{ fontSize: 10, fill: '#64748b' }} />
                <YAxis
                  type="category"
                  dataKey="name"
                  stroke="#475569"
                  tick={{ fontSize: 11, fill: '#94a3b8' }}
                  width={100}
                />
                <Tooltip
                  contentStyle={{
                    backgroundColor: '#1e293b',
                    border: '1px solid #334155',
                    borderRadius: '6px',
                    fontSize: '11px',
                  }}
                  formatter={(value: number) => [value, 'Events']}
                />
                <Bar dataKey="value" isAnimationActive={false} radius={[0, 4, 4, 0]}>
                  {barData.map((entry, i) => (
                    <Cell key={i} fill={entry.color} fillOpacity={0.8} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>

          {/* Rapid Entry Readiness checklist */}
          <div style={{
            backgroundColor: '#1e293b',
            borderRadius: '6px',
            padding: '12px 14px',
            marginTop: '8px',
          }}>
            <div style={{ fontSize: '13px', fontWeight: 600, color: '#e2e8f0', marginBottom: '8px' }}>
              Rapid Entry Readiness
            </div>

            {checkItems.map((c, i) => (
              <div key={i} style={{
                display: 'flex',
                alignItems: 'center',
                gap: '8px',
                padding: '3px 0',
                fontSize: '12px',
              }}>
                <span style={{ color: c.pass ? '#22c55e' : '#ef4444', fontWeight: 700 }}>
                  {c.pass ? 'PASS' : 'FAIL'}
                </span>
                <span style={{ color: '#cbd5e1' }}>{c.label}</span>
              </div>
            ))}

            <div style={{
              marginTop: '8px',
              paddingTop: '8px',
              borderTop: '1px solid #334155',
              fontSize: '12px',
              fontWeight: 600,
              color: allPassed ? '#22c55e' : '#eab308',
            }}>
              {allPassed
                ? 'All conditions met. Rapid Entry: ACTIVATABLE'
                : 'Conditions not yet met. Rapid Entry: NOT READY'}
            </div>
          </div>
        </>
      )}
    </div>
  );
};

export default PrelimAccuracy;
