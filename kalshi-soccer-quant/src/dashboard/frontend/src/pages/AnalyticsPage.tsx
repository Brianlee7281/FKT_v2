import React from 'react';
import { useAnalytics } from '../hooks/useAnalytics';
import { ALERT_COLORS } from '../utils/colors';
import { formatTime } from '../utils/formatters';
import HealthDashboard from '../components/Layer3_Analytics/HealthDashboard';
import CalibrationPlot from '../components/Layer3_Analytics/CalibrationPlot';
import CumulativePnL from '../components/Layer3_Analytics/CumulativePnL';
import DirectionalAnalysis from '../components/Layer3_Analytics/DirectionalAnalysis';
import Bet365Effect from '../components/Layer3_Analytics/Bet365Effect';
import PrelimAccuracy from '../components/Layer3_Analytics/PrelimAccuracy';
import ParamHistory from '../components/Layer3_Analytics/ParamHistory';

/** Layer 3: Analytics — scrollable page with all analytics sections. */
const AnalyticsPage: React.FC = () => {
  const { alerts } = useAnalytics();

  return (
    <div>
      <h2 style={{ fontSize: '18px', fontWeight: 600, marginBottom: '16px' }}>
        Analytics
      </h2>

      {/* 3A: Health Dashboard */}
      <Section>
        <HealthDashboard />
      </Section>

      {/* 3B: Calibration Plot */}
      <Section>
        <CalibrationPlot />
      </Section>

      {/* 3C: Cumulative P&L + Drawdown */}
      <Section>
        <CumulativePnL />
      </Section>

      {/* 3D: Directional Analysis */}
      <Section>
        <DirectionalAnalysis />
      </Section>

      {/* 3E: bet365 Alignment Effect */}
      <Section>
        <Bet365Effect />
      </Section>

      {/* 3F: Preliminary Accuracy */}
      <Section>
        <PrelimAccuracy />
      </Section>

      {/* 3G: Parameter History */}
      <Section>
        <ParamHistory />
      </Section>

      {/* Recent Alerts */}
      <Section>
        <span style={{ fontSize: '14px', fontWeight: 600, color: '#e2e8f0' }}>
          Recent Alerts
        </span>
        <div style={{ marginTop: '8px' }}>
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
        </div>
      </Section>
    </div>
  );
};

const Section: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <div style={{ marginBottom: '24px' }}>
    {children}
  </div>
);

export default AnalyticsPage;
