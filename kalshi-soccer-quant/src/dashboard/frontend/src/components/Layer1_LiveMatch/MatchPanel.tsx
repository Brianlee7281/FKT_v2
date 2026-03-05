import React, { useEffect, useState } from 'react';
import type { ActiveMatch, MatchState } from '../../types';
import { useMatchStream } from '../../hooks/useMatchStream';
import MatchHeader from './MatchHeader';
import PriceChart from './PriceChart';
import EventLog from './EventLog';
import MuChart from './MuChart';

const API_BASE = process.env.REACT_APP_API_URL || 'http://localhost:8000';

interface MatchPanelProps {
  match: ActiveMatch;
  /** Panel is in full-width focus mode. */
  focused?: boolean;
  /** Click handler for focus/unfocus toggle. */
  onClick?: () => void;
}

/**
 * D2.4 + D2.5: MatchPanel Container — per-match panel assembling Layer 1 components.
 *
 * Uses WebSocket for real-time state, falls back to REST polling.
 * Assembles MatchHeader (1A) + PriceChart (1B) + EventLog (1E).
 *
 * D2.5 behaviors:
 *   - Clicking the header area triggers onClick (focus toggle)
 *   - HALFTIME panels auto-minimize to header only (unless focused)
 */
const MatchPanel: React.FC<MatchPanelProps> = ({ match, focused, onClick }) => {
  const { state: wsState, connected } = useMatchStream(match.match_id);
  const [restState, setRestState] = useState<MatchState | null>(null);

  // REST fallback: poll match detail if WebSocket isn't providing data
  useEffect(() => {
    if (connected && wsState) return; // WS is live, skip REST polling

    const fetchDetail = async () => {
      try {
        const resp = await fetch(`${API_BASE}/api/matches/${match.match_id}`);
        if (resp.ok) setRestState(await resp.json());
      } catch {
        // Silently fail
      }
    };

    fetchDetail();
    const interval = setInterval(fetchDetail, 3000);
    return () => clearInterval(interval);
  }, [match.match_id, connected, wsState]);

  // Prefer WebSocket state, fall back to REST
  const state: MatchState | null = wsState || restState;

  if (!state) {
    return (
      <div style={{
        backgroundColor: '#1e293b',
        borderRadius: '8px',
        padding: '16px',
        border: '2px solid #334155',
        cursor: 'pointer',
      }} onClick={onClick}>
        <span style={{ color: '#94a3b8', fontSize: '14px' }}>
          Loading {match.home} vs {match.away}...
        </span>
      </div>
    );
  }

  // Ensure home/away are populated (REST detail may not include them)
  const fullState: MatchState = {
    ...state,
    home: state.home || match.home,
    away: state.away || match.away,
    match_id: state.match_id || match.match_id,
  };

  // HALFTIME auto-minimize: show header only (unless in focus mode)
  const isHalftime =
    fullState.engine_phase === 'HALFTIME' || fullState.engine_phase === 'halftime';
  const minimized = isHalftime && !focused;

  return (
    <div>
      {/* Clickable header area for focus toggle */}
      <div onClick={onClick} style={{ cursor: 'pointer' }}>
        <MatchHeader state={fullState} />
      </div>
      {!minimized && (
        <>
          <PriceChart state={fullState} />
          <MuChart state={fullState} />
          <EventLog state={fullState} />
        </>
      )}
      {minimized && (
        <div style={{
          padding: '6px 12px',
          fontSize: '11px',
          color: '#64748b',
          textAlign: 'center',
        }}>
          Halftime — click to expand
        </div>
      )}
    </div>
  );
};

export default MatchPanel;
