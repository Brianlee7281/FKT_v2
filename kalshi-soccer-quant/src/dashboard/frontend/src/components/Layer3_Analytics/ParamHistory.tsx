import React, { useEffect, useState } from 'react';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from 'recharts';
import type { TradingParams } from '../../types';

const API_BASE = process.env.REACT_APP_API_URL || 'http://localhost:8000';

interface HistoryPoint {
  timestamp: number;
  K_frac: number;
  z: number;
}

interface ApiResponse {
  history: HistoryPoint[];
  current: TradingParams;
}

/** Param rows to display in the current parameters table. */
const PARAM_ROWS: { key: keyof TradingParams; label: string; format: (v: unknown) => string }[] = [
  { key: 'K_frac', label: 'K_frac', format: (v) => Number(v).toFixed(3) },
  { key: 'z', label: 'z', format: (v) => Number(v).toFixed(3) },
  { key: 'theta_entry', label: 'theta_entry', format: (v) => Number(v).toFixed(4) },
  { key: 'theta_exit', label: 'theta_exit', format: (v) => Number(v).toFixed(4) },
  { key: 'cooldown_seconds', label: 'Cooldown (s)', format: (v) => String(v) },
  { key: 'low_confidence_multiplier', label: 'Low conf. mult.', format: (v) => Number(v).toFixed(2) },
  { key: 'rapid_entry_enabled', label: 'Rapid Entry', format: (v) => v ? 'ON' : 'OFF' },
  { key: 'bet365_divergence_auto_exit', label: 'Divergence Auto-Exit', format: (v) => v ? 'ON' : 'OFF' },
  { key: 'f_order_cap', label: 'f_order_cap (L1)', format: (v) => `${(Number(v) * 100).toFixed(0)}%` },
  { key: 'f_match_cap', label: 'f_match_cap (L2)', format: (v) => `${(Number(v) * 100).toFixed(0)}%` },
  { key: 'f_total_cap', label: 'f_total_cap (L3)', format: (v) => `${(Number(v) * 100).toFixed(0)}%` },
  { key: 'trading_mode', label: 'Trading Mode', format: (v) => String(v).toUpperCase() },
];

/**
 * 3G: Parameter History — K_frac and z evolution + current parameters table.
 *
 * Two small line charts: K_frac over time, z over time
 * Current Parameters table with all trading params
 *
 * Data: REST /api/analytics/params/history polled every 30s.
 */
const ParamHistory: React.FC = () => {
  const [history, setHistory] = useState<HistoryPoint[]>([]);
  const [current, setCurrent] = useState<TradingParams | null>(null);

  useEffect(() => {
    const fetchData = async () => {
      try {
        const resp = await fetch(`${API_BASE}/api/analytics/params/history`);
        if (resp.ok) {
          const data: ApiResponse = await resp.json();
          setHistory(data.history || []);
          if (data.current && Object.keys(data.current).length > 0) {
            setCurrent(data.current as TradingParams);
          }
        }
      } catch {
        // Retry on next poll
      }
    };

    fetchData();
    const interval = setInterval(fetchData, 30000);
    return () => clearInterval(interval);
  }, []);

  const hasHistory = history.length > 1;

  return (
    <div>
      <span style={{ fontSize: '14px', fontWeight: 600, color: '#e2e8f0' }}>
        Parameter History
      </span>

      {/* K_frac and z charts side by side */}
      {hasHistory && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px', marginTop: '8px' }}>
          <MiniParamChart data={history} dataKey="K_frac" label="K_frac" color="#3b82f6" />
          <MiniParamChart data={history} dataKey="z" label="z" color="#a855f7" />
        </div>
      )}

      {/* Current Parameters table */}
      {current ? (
        <div style={{
          backgroundColor: '#1e293b',
          borderRadius: '6px',
          padding: '12px 14px',
          marginTop: '8px',
        }}>
          <div style={{ fontSize: '13px', fontWeight: 600, color: '#e2e8f0', marginBottom: '8px' }}>
            Current Parameters
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '2px 16px' }}>
            {PARAM_ROWS.map((row) => {
              const val = current[row.key];
              if (val === undefined) return null;
              return (
                <div key={row.key} style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  padding: '3px 0',
                  fontSize: '12px',
                  borderBottom: '1px solid #334155',
                }}>
                  <span style={{ color: '#94a3b8' }}>{row.label}</span>
                  <span style={{ color: '#e2e8f0', fontWeight: 600 }}>{row.format(val)}</span>
                </div>
              );
            })}
          </div>
        </div>
      ) : (
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
          Loading parameters...
        </div>
      )}
    </div>
  );
};

/** Small line chart for a single parameter over time. */
const MiniParamChart: React.FC<{
  data: HistoryPoint[];
  dataKey: string;
  label: string;
  color: string;
}> = ({ data, dataKey, label, color }) => (
  <div style={{
    backgroundColor: '#0f172a',
    borderRadius: '6px',
    border: '1px solid #334155',
    padding: '8px 4px 4px 0',
  }}>
    <div style={{ fontSize: '11px', color: '#94a3b8', fontWeight: 600, paddingLeft: '12px', marginBottom: '4px' }}>
      {label}
    </div>
    <ResponsiveContainer width="100%" height={120}>
      <LineChart data={data} margin={{ top: 5, right: 10, left: 0, bottom: 5 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
        <XAxis
          dataKey="timestamp"
          type="number"
          domain={['dataMin', 'dataMax']}
          stroke="#475569"
          tick={{ fontSize: 9, fill: '#64748b' }}
          tickFormatter={(ts: number) => {
            if (!ts) return '';
            const d = new Date(ts * 1000);
            return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
          }}
        />
        <YAxis
          stroke="#475569"
          tick={{ fontSize: 9, fill: '#64748b' }}
          width={40}
          domain={['auto', 'auto']}
        />
        <Tooltip
          contentStyle={{
            backgroundColor: '#1e293b',
            border: '1px solid #334155',
            borderRadius: '6px',
            fontSize: '11px',
          }}
          labelFormatter={(ts: number) => {
            if (!ts) return '';
            return new Date(ts * 1000).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
          }}
        />
        <Line
          dataKey={dataKey}
          type="monotone"
          stroke={color}
          strokeWidth={2}
          dot={false}
          isAnimationActive={false}
        />
      </LineChart>
    </ResponsiveContainer>
  </div>
);

export default ParamHistory;
