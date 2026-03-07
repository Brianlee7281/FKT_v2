import React, { useEffect, useState } from 'react';
import type { HealthDashboard as HealthData } from '../../types';

const API_BASE = process.env.REACT_APP_API_URL || '';

/** Status -> color mapping for traffic-light gauges. */
const STATUS_COLORS: Record<string, string> = {
  healthy: '#22c55e',
  warning: '#eab308',
  risk: '#ef4444',
  pending: '#64748b',
};

/** Status -> icon mapping. */
const STATUS_ICONS: Record<string, string> = {
  healthy: 'G',
  warning: 'Y',
  risk: 'R',
  pending: '--',
};

/**
 * 3A: Health Dashboard — 7 gauge indicators with traffic-light colors.
 *
 * Metrics:
 *   Brier Score, DBS vs Pinnacle, Edge Realization, Max Drawdown,
 *   Alignment Value, Preliminary Accuracy, No-dir Edge Real.
 *
 * Overall status: HEALTHY / WARNING / AT RISK / PENDING
 *
 * Data: REST /api/analytics/health polled every 30s.
 */
const HealthDashboard: React.FC = () => {
  const [data, setData] = useState<HealthData | null>(null);

  useEffect(() => {
    const fetch_ = async () => {
      try {
        const resp = await fetch(`${API_BASE}/api/analytics/health`);
        if (resp.ok) setData(await resp.json());
      } catch {
        // Retry on next poll
      }
    };

    fetch_();
    const interval = setInterval(fetch_, 30000);
    return () => clearInterval(interval);
  }, []);

  if (!data) {
    return (
      <div style={{ textAlign: 'center', padding: '40px', color: '#64748b' }}>
        Loading health metrics...
      </div>
    );
  }

  const overallColor = STATUS_COLORS[data.overall_status] || STATUS_COLORS.pending;
  const overallLabel = data.overall_status === 'healthy' ? 'HEALTHY'
    : data.overall_status === 'warning' ? 'WARNING'
    : data.overall_status === 'risk' ? 'AT RISK'
    : 'PENDING';

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
        <span style={{ fontSize: '14px', fontWeight: 600, color: '#e2e8f0' }}>
          System Health
        </span>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <span style={{
            display: 'inline-block',
            width: '8px',
            height: '8px',
            borderRadius: '50%',
            backgroundColor: overallColor,
          }} />
          <span style={{ fontSize: '13px', fontWeight: 700, color: overallColor }}>
            {overallLabel}
          </span>
          <span style={{ fontSize: '11px', color: '#64748b' }}>
            ({data.total_trades} trades)
          </span>
        </div>
      </div>

      {/* Metric gauges grid */}
      <div style={{
        display: 'grid',
        gap: '8px',
        gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))',
      }}>
        {data.metrics.map((m) => {
          const color = STATUS_COLORS[m.status] || STATUS_COLORS.pending;
          const icon = STATUS_ICONS[m.status] || '--';
          return (
            <div
              key={m.name}
              style={{
                backgroundColor: '#1e293b',
                borderRadius: '6px',
                padding: '12px 14px',
                borderLeft: `3px solid ${color}`,
              }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '4px' }}>
                <span style={{ fontSize: '12px', color: '#94a3b8', fontWeight: 600 }}>
                  {m.name}
                </span>
                <span style={{
                  fontSize: '10px',
                  fontWeight: 700,
                  color,
                  backgroundColor: color + '20',
                  padding: '1px 6px',
                  borderRadius: '3px',
                }}>
                  {icon}
                </span>
              </div>
              <div style={{ fontSize: '20px', fontWeight: 700, color: m.value !== null ? '#e2e8f0' : '#475569' }}>
                {m.value !== null ? formatMetricValue(m.name, m.value) : '--'}
              </div>
              <div style={{ fontSize: '10px', color: '#64748b', marginTop: '2px' }}>
                Threshold: {m.threshold}
              </div>
            </div>
          );
        })}
      </div>

      {/* Note */}
      {data.note && (
        <div style={{ fontSize: '11px', color: '#64748b', marginTop: '8px', fontStyle: 'italic' }}>
          {data.note}
        </div>
      )}
    </div>
  );
};

/** Format metric value based on metric name. */
function formatMetricValue(name: string, value: number): string {
  if (name.includes('Drawdown')) return `${value.toFixed(1)}%`;
  if (name.includes('Alignment Value')) return `${value >= 0 ? '+' : ''}${value.toFixed(1)}c`;
  if (name.includes('Accuracy')) return value.toFixed(3);
  return value.toFixed(3);
}

export default HealthDashboard;
