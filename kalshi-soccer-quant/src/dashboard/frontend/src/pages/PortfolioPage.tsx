import React from 'react';
import { usePortfolio } from '../hooks/usePortfolio';
import { formatPnl, formatPct } from '../utils/formatters';

/** Layer 2: Portfolio View — aggregate exposure, P&L, risk limits. */
const PortfolioPage: React.FC = () => {
  const { summary, matches, connected } = usePortfolio();

  if (!summary) {
    return (
      <div style={{ textAlign: 'center', padding: '60px 20px', color: '#94a3b8' }}>
        <p>Loading portfolio data...</p>
      </div>
    );
  }

  return (
    <div>
      <h2 style={{ fontSize: '18px', fontWeight: 600, marginBottom: '16px' }}>
        Portfolio Overview
      </h2>

      {/* Summary Cards */}
      <div style={{ display: 'grid', gap: '12px', gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))', marginBottom: '24px' }}>
        <SummaryCard label="Bankroll" value={`$${summary.bankroll.toLocaleString()}`} />
        <SummaryCard label="Active Matches" value={String(summary.active_matches)} />
        <SummaryCard label="Open Positions" value={String(summary.open_positions)} />
        <SummaryCard
          label="Total Exposure"
          value={`$${summary.total_exposure} (${formatPct(summary.total_exposure_pct)})`}
        />
        <SummaryCard
          label="Realized P&L"
          value={formatPnl(summary.realized_pnl)}
          valueColor={summary.realized_pnl >= 0 ? '#22c55e' : '#ef4444'}
        />
        <SummaryCard
          label="Unrealized P&L"
          value={formatPnl(summary.unrealized_pnl)}
          valueColor={summary.unrealized_pnl >= 0 ? '#22c55e' : '#ef4444'}
        />
      </div>

      {/* Risk Limits */}
      <h3 style={{ fontSize: '15px', fontWeight: 600, marginBottom: '12px', color: '#94a3b8' }}>
        Risk Limits
      </h3>
      <div style={{ display: 'grid', gap: '12px', gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))', marginBottom: '24px' }}>
        <SummaryCard label="L1 Order Cap" value={formatPct(summary.risk_limits.l1_order_cap * 100)} />
        <SummaryCard label="L2 Match Cap" value={formatPct(summary.risk_limits.l2_match_cap * 100)} />
        <SummaryCard label="L3 Total Cap" value={formatPct(summary.risk_limits.l3_total_cap * 100)} />
        <SummaryCard
          label="L3 Used"
          value={formatPct(summary.risk_limits.l3_used_pct)}
          valueColor={summary.risk_limits.l3_used_pct > 80 ? '#ef4444' : '#22c55e'}
        />
      </div>

      {/* Active Matches via WebSocket */}
      <h3 style={{ fontSize: '15px', fontWeight: 600, marginBottom: '12px', color: '#94a3b8' }}>
        Live Match States ({Object.keys(matches).length})
        {!connected && <span style={{ color: '#ef4444', marginLeft: '8px' }}>(disconnected)</span>}
      </h3>
      {Object.keys(matches).length === 0 ? (
        <p style={{ color: '#64748b', fontSize: '14px' }}>
          No live match data streaming. Matches will appear when engines are active.
        </p>
      ) : (
        <div style={{ display: 'grid', gap: '8px' }}>
          {Object.entries(matches).map(([id, state]) => (
            <div
              key={id}
              style={{
                backgroundColor: '#1e293b',
                borderRadius: '6px',
                padding: '12px 16px',
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                fontSize: '14px',
              }}
            >
              <span style={{ fontWeight: 600 }}>
                {state.home} vs {state.away}
              </span>
              <span style={{ color: '#94a3b8' }}>{state.status}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

const SummaryCard: React.FC<{
  label: string;
  value: string;
  valueColor?: string;
}> = ({ label, value, valueColor }) => (
  <div
    style={{
      backgroundColor: '#1e293b',
      borderRadius: '8px',
      padding: '16px',
    }}
  >
    <div style={{ fontSize: '12px', color: '#64748b', marginBottom: '4px' }}>
      {label}
    </div>
    <div style={{ fontSize: '18px', fontWeight: 700, color: valueColor || '#e2e8f0' }}>
      {value}
    </div>
  </div>
);

export default PortfolioPage;
