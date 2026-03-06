import React from 'react';
import { usePortfolio } from '../hooks/usePortfolio';
import RiskDashboard from '../components/Layer2_Portfolio/RiskDashboard';
import PositionTable from '../components/Layer2_Portfolio/PositionTable';
import PnLTimeline from '../components/Layer2_Portfolio/PnLTimeline';

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

      {/* D3.4: Risk Dashboard with summary metrics + progress bars */}
      <div style={{ marginBottom: '24px' }}>
        <RiskDashboard summary={summary} />
      </div>

      {/* D3.5: Position Table */}
      <div style={{ marginBottom: '24px' }}>
        <PositionTable />
      </div>

      {/* D3.6: P&L Timeline */}
      <div style={{ marginBottom: '24px' }}>
        <PnLTimeline />
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

export default PortfolioPage;
