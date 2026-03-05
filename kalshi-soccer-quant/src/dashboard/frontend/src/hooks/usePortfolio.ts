import { useEffect, useRef, useState, useCallback } from 'react';
import type { MatchState, PortfolioSummary } from '../types';

const WS_BASE = process.env.REACT_APP_WS_URL || 'ws://localhost:8000';
const API_BASE = process.env.REACT_APP_API_URL || 'http://localhost:8000';
const RECONNECT_DELAY = 2000;

/**
 * Combined hook for portfolio data.
 *
 * - WebSocket /ws/portfolio for real-time match state updates.
 * - REST /api/portfolio/summary polled every 5s for aggregate metrics.
 */
export function usePortfolio() {
  const [matches, setMatches] = useState<Record<string, MatchState>>({});
  const [summary, setSummary] = useState<PortfolioSummary | null>(null);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>();

  // WebSocket: all match states
  const connect = useCallback(() => {
    const ws = new WebSocket(`${WS_BASE}/ws/portfolio`);
    wsRef.current = ws;

    ws.onopen = () => setConnected(true);

    ws.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        if (payload.type === 'keepalive') return;

        const data: MatchState = payload.data || payload;
        const matchId = data.match_id;
        if (matchId) {
          setMatches((prev) => ({ ...prev, [matchId]: data }));
        }
      } catch {
        // Ignore malformed messages
      }
    };

    ws.onclose = () => {
      setConnected(false);
      reconnectTimer.current = setTimeout(connect, RECONNECT_DELAY);
    };

    ws.onerror = () => ws.close();
  }, []);

  useEffect(() => {
    connect();
    return () => {
      clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
  }, [connect]);

  // REST: portfolio summary (polled)
  useEffect(() => {
    const fetchSummary = async () => {
      try {
        const resp = await fetch(`${API_BASE}/api/portfolio/summary`);
        if (resp.ok) {
          setSummary(await resp.json());
        }
      } catch {
        // Silently fail — will retry
      }
    };

    fetchSummary();
    const interval = setInterval(fetchSummary, 5000);
    return () => clearInterval(interval);
  }, []);

  return { matches, summary, connected };
}
