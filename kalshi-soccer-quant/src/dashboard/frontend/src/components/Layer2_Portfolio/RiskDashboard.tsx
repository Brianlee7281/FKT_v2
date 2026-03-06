import React from 'react';
import type { PortfolioSummary } from '../../types';
import { formatPnl } from '../../utils/formatters';

interface RiskDashboardProps {
  summary: PortfolioSummary;
}

/**
 * 2A: Risk Dashboard — visual progress bars for 3-Layer risk limits.
 *
 * Layout:
 *   Summary metrics row: Bankroll | Active Matches | Open Positions | Exposure | Unrealized P&L
 *   Risk bars:
 *     L1 (Order 3%):   progress bar  $87 / $150
 *     L2 (Match 5%):   progress bar  per-match
 *     L3 (Total 20%):  progress bar  $412 / $1,000
 *
 * Color: green (<50%), yellow (50-80%), red (>80%)
 */
const RiskDashboard: React.FC<RiskDashboardProps> = ({ summary }) => {
  const { risk_limits } = summary;

  // Compute dollar amounts from percentages
  const l1Cap = summary.bankroll * risk_limits.l1_order_cap;
  const l2Cap = summary.bankroll * risk_limits.l2_match_cap;
  const l3Cap = summary.bankroll * risk_limits.l3_total_cap;
  const l3Used = (risk_limits.l3_used_pct / 100) * l3Cap;

  // Exposure as percentage of L3 cap for the main bar
  const exposurePct = l3Cap > 0 ? (summary.total_exposure / l3Cap) * 100 : 0;

  return (
    <div>
      {/* Summary Metrics */}
      <div style={{
        display: 'grid',
        gap: '10px',
        gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))',
        marginBottom: '20px',
      }}>
        <MetricCard label="Bankroll" value={`$${summary.bankroll.toLocaleString()}`} />
        <MetricCard label="Active Matches" value={String(summary.active_matches)} />
        <MetricCard label="Open Positions" value={String(summary.open_positions)} />
        <MetricCard
          label="Exposure"
          value={`$${summary.total_exposure.toLocaleString()}`}
          sub={`${summary.total_exposure_pct.toFixed(1)}% of bankroll`}
        />
        <MetricCard
          label="Realized P&L"
          value={formatPnl(summary.realized_pnl)}
          valueColor={summary.realized_pnl >= 0 ? '#22c55e' : '#ef4444'}
        />
        <MetricCard
          label="Unrealized P&L"
          value={formatPnl(summary.unrealized_pnl)}
          valueColor={summary.unrealized_pnl >= 0 ? '#22c55e' : '#ef4444'}
        />
      </div>

      {/* Risk Limit Bars */}
      <div style={{
        backgroundColor: '#1e293b',
        borderRadius: '8px',
        padding: '16px',
      }}>
        <h3 style={{ fontSize: '14px', fontWeight: 600, color: '#e2e8f0', marginTop: 0, marginBottom: '14px' }}>
          Risk Limits
        </h3>

        <RiskBar
          label="L1 — Order Cap"
          pctLabel={`${(risk_limits.l1_order_cap * 100).toFixed(0)}%`}
          capDollars={l1Cap}
          description="Max single order size"
        />

        <RiskBar
          label="L2 — Match Cap"
          pctLabel={`${(risk_limits.l2_match_cap * 100).toFixed(0)}%`}
          capDollars={l2Cap}
          description="Max exposure per match"
        />

        <RiskBar
          label="L3 — Total Cap"
          pctLabel={`${(risk_limits.l3_total_cap * 100).toFixed(0)}%`}
          usedDollars={l3Used}
          capDollars={l3Cap}
          usedPct={risk_limits.l3_used_pct}
          description="Total portfolio exposure limit"
        />

        {/* Overall exposure bar */}
        <div style={{ marginTop: '12px', paddingTop: '12px', borderTop: '1px solid #334155' }}>
          <RiskBar
            label="Current Exposure"
            usedDollars={summary.total_exposure}
            capDollars={l3Cap}
            usedPct={exposurePct}
            description={`$${summary.total_exposure.toLocaleString()} / $${l3Cap.toLocaleString()}`}
          />
        </div>
      </div>
    </div>
  );
};

/** Summary metric card. */
const MetricCard: React.FC<{
  label: string;
  value: string;
  valueColor?: string;
  sub?: string;
}> = ({ label, value, valueColor, sub }) => (
  <div style={{
    backgroundColor: '#1e293b',
    borderRadius: '8px',
    padding: '14px',
  }}>
    <div style={{ fontSize: '11px', color: '#64748b', marginBottom: '4px', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
      {label}
    </div>
    <div style={{ fontSize: '18px', fontWeight: 700, color: valueColor || '#e2e8f0' }}>
      {value}
    </div>
    {sub && (
      <div style={{ fontSize: '11px', color: '#64748b', marginTop: '2px' }}>
        {sub}
      </div>
    )}
  </div>
);

/** Risk limit progress bar. */
const RiskBar: React.FC<{
  label: string;
  pctLabel?: string;
  usedDollars?: number;
  capDollars: number;
  usedPct?: number;
  description: string;
}> = ({ label, pctLabel, usedDollars, capDollars, usedPct, description }) => {
  // If no usage data, show the limit info only
  const hasUsage = usedPct !== undefined && usedDollars !== undefined;
  const pct = hasUsage ? Math.min(usedPct, 100) : 0;
  const barColor = pct < 50 ? '#22c55e' : pct < 80 ? '#eab308' : '#ef4444';

  return (
    <div style={{ marginBottom: '12px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: '4px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
          <span style={{ fontSize: '13px', fontWeight: 600, color: '#e2e8f0' }}>
            {label}
          </span>
          {pctLabel && (
            <span style={{ fontSize: '11px', color: '#64748b' }}>
              ({pctLabel})
            </span>
          )}
        </div>
        <span style={{ fontSize: '12px', color: '#94a3b8' }}>
          {hasUsage
            ? `$${usedDollars.toLocaleString()} / $${capDollars.toLocaleString()}`
            : `$${capDollars.toLocaleString()}`
          }
        </span>
      </div>

      {/* Progress bar */}
      <div style={{
        width: '100%',
        height: '8px',
        backgroundColor: '#334155',
        borderRadius: '4px',
        overflow: 'hidden',
      }}>
        <div style={{
          width: hasUsage ? `${pct}%` : '0%',
          height: '100%',
          backgroundColor: hasUsage ? barColor : '#334155',
          borderRadius: '4px',
          transition: 'width 0.3s ease',
        }} />
      </div>

      <div style={{ fontSize: '10px', color: '#64748b', marginTop: '2px' }}>
        {description}
      </div>
    </div>
  );
};

export default RiskDashboard;
