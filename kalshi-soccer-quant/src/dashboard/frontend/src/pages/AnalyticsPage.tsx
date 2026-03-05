import React from 'react';
import { useAnalytics } from '../hooks/useAnalytics';
import { ALERT_COLORS } from '../utils/colors';
import { formatTime } from '../utils/formatters';

/** Layer 3: Analytics — health dashboard, P&L, params, alerts. */
const AnalyticsPage: React.FC = () => {
  const { health, cumulativePnl, params, alerts } = useAnalytics();

  return (
    <div>
      <h2 style={{ fontSize: '18px', fontWeight: 600, marginBottom: '16px' }}>
        Analytics
      </h2>

      {/* Health Dashboard */}
      <Section title="System Health">
        {health ? (
          <div style={{ display: 'grid', gap: '8px', gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))' }}>
            {health.metrics.map((m) => (
              <div
                key={m.name}
                style={{
                  backgroundColor: '#1e293b',
                  borderRadius: '6px',
                  padding: '12px 16px',
                  borderLeft: `3px solid ${statusColor(m.status)}`,
                }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <span style={{ fontSize: '14px', fontWeight: 500 }}>{m.name}</span>
                  <StatusBadge status={m.status} />
                </div>
                <div style={{ fontSize: '12px', color: '#64748b', marginTop: '4px' }}>
                  Target: {m.threshold}
                </div>
                <div style={{ fontSize: '20px', fontWeight: 700, marginTop: '4px' }}>
                  {m.value !== null ? m.value : '--'}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <Loading />
        )}
      </Section>

      {/* Cumulative P&L */}
      <Section title="Cumulative P&L">
        {cumulativePnl ? (
          <div style={{ backgroundColor: '#1e293b', borderRadius: '6px', padding: '16px' }}>
            <div style={{ fontSize: '14px', color: '#94a3b8', marginBottom: '8px' }}>
              Max Drawdown: {cumulativePnl.max_drawdown_pct}%
            </div>
            {cumulativePnl.series.length === 0 ? (
              <p style={{ color: '#64748b', fontSize: '14px' }}>
                No trade data yet. P&L chart will populate as trades are executed.
              </p>
            ) : (
              <p style={{ color: '#64748b', fontSize: '14px' }}>
                {cumulativePnl.series.length} data points.
                Chart visualization will be added in Sprint D5.
              </p>
            )}
          </div>
        ) : (
          <Loading />
        )}
      </Section>

      {/* Current Parameters */}
      <Section title="Trading Parameters">
        {params ? (
          <div style={{ display: 'grid', gap: '8px', gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))' }}>
            {Object.entries(params).map(([key, value]) => (
              <div
                key={key}
                style={{
                  backgroundColor: '#1e293b',
                  borderRadius: '6px',
                  padding: '10px 14px',
                }}
              >
                <div style={{ fontSize: '11px', color: '#64748b' }}>{key}</div>
                <div style={{ fontSize: '15px', fontWeight: 600, marginTop: '2px' }}>
                  {String(value)}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <Loading />
        )}
      </Section>

      {/* Recent Alerts */}
      <Section title="Recent Alerts">
        {alerts.length === 0 ? (
          <p style={{ color: '#64748b', fontSize: '14px' }}>No alerts yet.</p>
        ) : (
          <div style={{ display: 'grid', gap: '6px' }}>
            {alerts.map((a, i) => (
              <div
                key={i}
                style={{
                  backgroundColor: '#1e293b',
                  borderRadius: '6px',
                  padding: '10px 14px',
                  borderLeft: `3px solid ${ALERT_COLORS[a.severity] || '#64748b'}`,
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                }}
              >
                <div>
                  <span style={{ fontWeight: 600, fontSize: '13px' }}>{a.title}</span>
                  {a.body && (
                    <span style={{ color: '#94a3b8', fontSize: '13px', marginLeft: '8px' }}>
                      {a.body}
                    </span>
                  )}
                </div>
                <span style={{ color: '#64748b', fontSize: '12px', whiteSpace: 'nowrap' }}>
                  {formatTime(a.timestamp)}
                </span>
              </div>
            ))}
          </div>
        )}
      </Section>
    </div>
  );
};

const Section: React.FC<{ title: string; children: React.ReactNode }> = ({ title, children }) => (
  <div style={{ marginBottom: '28px' }}>
    <h3 style={{ fontSize: '15px', fontWeight: 600, color: '#94a3b8', marginBottom: '12px' }}>
      {title}
    </h3>
    {children}
  </div>
);

const StatusBadge: React.FC<{ status: string }> = ({ status }) => (
  <span
    style={{
      backgroundColor: statusColor(status),
      color: '#fff',
      padding: '1px 8px',
      borderRadius: '10px',
      fontSize: '11px',
      fontWeight: 600,
      textTransform: 'uppercase',
    }}
  >
    {status}
  </span>
);

const Loading: React.FC = () => (
  <p style={{ color: '#64748b', fontSize: '14px' }}>Loading...</p>
);

function statusColor(status: string): string {
  switch (status) {
    case 'healthy': return '#22c55e';
    case 'warning': return '#eab308';
    case 'risk': return '#ef4444';
    default: return '#64748b';
  }
}

export default AnalyticsPage;
