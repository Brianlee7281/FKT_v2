import { useEffect, useRef, useState, useCallback } from 'react';
import type { MatchState } from '../types';

const WS_BASE = process.env.REACT_APP_WS_URL || 'ws://localhost:8000';
const RECONNECT_DELAY = 2000;

/**
 * WebSocket hook for a single match state stream.
 *
 * Connects to /ws/live/{matchId} and returns the latest MatchState.
 * Auto-reconnects on disconnect with a 2s delay.
 */
export function useMatchStream(matchId: string | null) {
  const [state, setState] = useState<MatchState | null>(null);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>();

  const connect = useCallback(() => {
    if (!matchId) return;

    const ws = new WebSocket(`${WS_BASE}/ws/live/${matchId}`);
    wsRef.current = ws;

    ws.onopen = () => setConnected(true);

    ws.onmessage = (event) => {
      try {
        const data: MatchState = JSON.parse(event.data);
        setState(data);
      } catch {
        // Ignore malformed messages
      }
    };

    ws.onclose = () => {
      setConnected(false);
      reconnectTimer.current = setTimeout(connect, RECONNECT_DELAY);
    };

    ws.onerror = () => {
      ws.close();
    };
  }, [matchId]);

  useEffect(() => {
    connect();
    return () => {
      clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
  }, [connect]);

  return { state, connected };
}
