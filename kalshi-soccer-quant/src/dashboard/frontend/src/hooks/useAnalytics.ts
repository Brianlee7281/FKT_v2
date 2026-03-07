import { useEffect, useState } from 'react';
import type {
  HealthDashboard,
  CalibrationBin,
  CumulativePnlPoint,
  TradingParams,
  Alert,
} from '../types';

const API_BASE = process.env.REACT_APP_API_URL || '';

async function fetchJson<T>(path: string): Promise<T | null> {
  try {
    const resp = await fetch(`${API_BASE}${path}`);
    if (resp.ok) return await resp.json();
  } catch {
    // Silently fail
  }
  return null;
}

/**
 * Hook for Layer 3 analytics data (REST, polled every 30s).
 */
export function useAnalytics() {
  const [health, setHealth] = useState<HealthDashboard | null>(null);
  const [calibration, setCalibration] = useState<CalibrationBin[]>([]);
  const [cumulativePnl, setCumulativePnl] = useState<{ series: CumulativePnlPoint[]; max_drawdown_pct: number } | null>(null);
  const [params, setParams] = useState<TradingParams | null>(null);
  const [alerts, setAlerts] = useState<Alert[]>([]);

  useEffect(() => {
    const fetchAll = async () => {
      const [h, c, p, pr, a] = await Promise.all([
        fetchJson<HealthDashboard>('/api/analytics/health'),
        fetchJson<{ bins: CalibrationBin[] }>('/api/analytics/calibration'),
        fetchJson<{ series: CumulativePnlPoint[]; max_drawdown_pct: number }>('/api/analytics/pnl_cumulative'),
        fetchJson<TradingParams>('/api/analytics/params/current'),
        fetchJson<Alert[]>('/api/analytics/alerts/recent'),
      ]);

      if (h) setHealth(h);
      if (c) setCalibration(c.bins);
      if (p) setCumulativePnl(p);
      if (pr) setParams(pr);
      if (a) setAlerts(a);
    };

    fetchAll();
    const interval = setInterval(fetchAll, 30000);
    return () => clearInterval(interval);
  }, []);

  return { health, calibration, cumulativePnl, params, alerts };
}
