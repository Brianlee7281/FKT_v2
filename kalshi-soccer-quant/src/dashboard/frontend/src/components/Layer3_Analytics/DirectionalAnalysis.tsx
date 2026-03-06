import React, { useEffect, useState } from 'react';

const API_BASE = process.env.REACT_APP_API_URL || 'http://localhost:8000';

interface DirectionStats {
  trades: number;
  win_rate: number;
  edge_realization: number;
  avg_ev_entry: number;
  avg_actual_return: number;
}

interface ApiResponse {
  buy_yes: DirectionStats;
  buy_no: DirectionStats;
}

/**
 * 3D: Directional Analysis — Buy Yes vs Buy No performance breakdown.
 *
 * Side-by-side panels showing:
 *   Trades, Win Rate, Edge Realization, Avg EV at Entry, Avg Actual Return
 *
 * Warning indicators:
 *   No-dir Edge Real > 1.5 -> "z is too conservative, consider lowering"
 *   No-dir Edge Real < 0.5 -> "z is too aggressive, consider raising"
 *
 * Data: REST /api/analytics/directional polled every 30s.
 */
const DirectionalAnalysis: React.FC = () => {
  const [data, setData] = useState<ApiResponse | null>(null);

  useEffect(() => {
    const fetchData = async () => {
      try {
        const resp = await fetch(`${API_BASE}/api/analytics/directional`);
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
        Loading directional analysis...
      </div>
    );
  }

  const totalTrades = data.buy_yes.trades + data.buy_no.trades;
  const hasData = totalTrades > 0;

  // Compute no-direction edge realization (weighted average)
  const noDir = totalTrades > 0
    ? (data.buy_yes.edge_realization * data.buy_yes.trades + data.buy_no.edge_realization * data.buy_no.trades) / totalTrades
    : 0;

  // Warning logic
  let warning: string | null = null;
  if (hasData && noDir > 1.5) {
    warning = 'No-dir Edge Realization > 1.5 — z is too conservative, consider lowering.';
  } else if (hasData && noDir < 0.5) {
    warning = 'No-dir Edge Realization < 0.5 — z is too aggressive, consider raising.';
  }

  return (
    <div>
      <span style={{ fontSize: '14px', fontWeight: 600, color: '#e2e8f0' }}>
        Directional Analysis
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
          No trade data yet. Directional breakdown will appear after trades settle.
        </div>
      ) : (
        <>
          {/* Side-by-side panels */}
          <div style={{
            display: 'grid',
            gridTemplateColumns: '1fr 1fr',
            gap: '8px',
            marginTop: '8px',
          }}>
            <DirectionPanel label="Buy Yes" stats={data.buy_yes} color="#22c55e" />
            <DirectionPanel label="Buy No" stats={data.buy_no} color="#ef4444" />
          </div>

          {/* Warning indicator */}
          {warning && (
            <div style={{
              marginTop: '8px',
              padding: '8px 12px',
              backgroundColor: '#78350f20',
              border: '1px solid #92400e',
              borderRadius: '6px',
              fontSize: '12px',
              color: '#eab308',
            }}>
              {warning}
            </div>
          )}
        </>
      )}
    </div>
  );
};

const DirectionPanel: React.FC<{
  label: string;
  stats: DirectionStats;
  color: string;
}> = ({ label, stats, color }) => {
  const edgeColor = stats.edge_realization >= 0.7 && stats.edge_realization <= 1.3
    ? '#22c55e'
    : stats.edge_realization >= 0.5
    ? '#eab308'
    : '#ef4444';

  return (
    <div style={{
      backgroundColor: '#1e293b',
      borderRadius: '6px',
      padding: '14px',
      borderTop: `3px solid ${color}`,
    }}>
      <div style={{ fontSize: '13px', fontWeight: 700, color, marginBottom: '10px' }}>
        {label}
      </div>

      <StatRow label="Trades" value={String(stats.trades)} />
      <StatRow label="Win Rate" value={`${(stats.win_rate * 100).toFixed(1)}%`} />
      <StatRow
        label="Edge Realization"
        value={stats.edge_realization.toFixed(2)}
        valueColor={edgeColor}
      />
      <StatRow label="Avg EV at Entry" value={`${(stats.avg_ev_entry * 100).toFixed(1)}c`} />
      <StatRow label="Avg Actual Return" value={`${(stats.avg_actual_return * 100).toFixed(1)}c`} />
    </div>
  );
};

const StatRow: React.FC<{
  label: string;
  value: string;
  valueColor?: string;
}> = ({ label, value, valueColor }) => (
  <div style={{
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'baseline',
    padding: '3px 0',
    fontSize: '12px',
  }}>
    <span style={{ color: '#94a3b8' }}>{label}</span>
    <span style={{ color: valueColor || '#e2e8f0', fontWeight: 600 }}>{value}</span>
  </div>
);

export default DirectionalAnalysis;
