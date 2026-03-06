import React from 'react';
import type { MatchState, SourceStatusInfo } from '../../types';

interface SourceStatusProps {
  state: MatchState | null;
}

/** Default sources shown when engine doesn't provide source status. */
const DEFAULT_SOURCES: SourceStatusInfo[] = [
  { name: 'Live Odds WS', type: 'websocket', connected: false },
  { name: 'Kalshi WS', type: 'websocket', connected: false },
  { name: 'Live Score', type: 'polling', connected: false },
];

/**
 * 1F: Source Status — compact bar showing data source health.
 *
 * Displays connection status for each data source feeding the engine:
 *   Live Odds WS:  green dot  Connected  <1s
 *   Kalshi WS:     green dot  Connected  ~1s
 *   Live Score:    green dot  Polling    3s cycle
 *
 * Status logic:
 *   green  = last message < 5s ago
 *   yellow = last message 5-10s ago
 *   red    = last message > 10s ago or error
 */
const SourceStatus: React.FC<SourceStatusProps> = ({ state }) => {
  if (!state) return null;

  const sources = state.sources && state.sources.length > 0
    ? state.sources
    : inferSources(state);

  return (
    <div style={{
      marginTop: '8px',
      backgroundColor: '#0f172a',
      border: '1px solid #334155',
      borderRadius: '6px',
      padding: '6px 10px',
      display: 'flex',
      gap: '16px',
      flexWrap: 'wrap',
      fontSize: '11px',
    }}>
      {sources.map((src) => {
        const freshness = getFreshness(src);
        return (
          <div key={src.name} style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
            {/* Status dot */}
            <span style={{
              display: 'inline-block',
              width: '7px',
              height: '7px',
              borderRadius: '50%',
              backgroundColor: freshness.color,
              flexShrink: 0,
            }} />
            {/* Source name */}
            <span style={{ color: '#94a3b8', fontWeight: 600 }}>
              {src.name}:
            </span>
            {/* Status label */}
            <span style={{ color: freshness.color }}>
              {freshness.label}
            </span>
            {/* Latency / cycle info */}
            {freshness.detail && (
              <span style={{ color: '#64748b' }}>
                {freshness.detail}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
};

interface Freshness {
  color: string;
  label: string;
  detail?: string;
}

function getFreshness(src: SourceStatusInfo): Freshness {
  if (src.error) {
    return { color: '#ef4444', label: 'Error', detail: src.error };
  }

  if (!src.connected) {
    return { color: '#ef4444', label: 'Disconnected' };
  }

  if (!src.last_message_ts) {
    return { color: '#22c55e', label: src.type === 'polling' ? 'Polling' : 'Connected' };
  }

  const ageSec = (Date.now() / 1000) - src.last_message_ts;

  if (ageSec < 5) {
    const detail = ageSec < 1 ? '<1s' : `${Math.floor(ageSec)}s`;
    return {
      color: '#22c55e',
      label: src.type === 'polling' ? 'Polling' : 'Connected',
      detail,
    };
  }

  if (ageSec < 10) {
    return {
      color: '#eab308',
      label: 'Stale',
      detail: `${Math.floor(ageSec)}s ago`,
    };
  }

  return {
    color: '#ef4444',
    label: 'Stale',
    detail: `${Math.floor(ageSec)}s ago`,
  };
}

/** Infer source status from available state fields when engine doesn't report sources. */
function inferSources(state: MatchState): SourceStatusInfo[] {
  const hasBet365 = state.P_bet365 && Object.keys(state.P_bet365).length > 0;
  const hasKalshi = state.P_kalshi_bid !== undefined || state.P_kalshi_ask !== undefined;
  const hasScore = state.score_h !== undefined;

  return [
    {
      name: 'Live Odds WS',
      type: 'websocket',
      connected: !!hasBet365,
    },
    {
      name: 'Kalshi WS',
      type: 'websocket',
      connected: !!hasKalshi,
    },
    {
      name: 'Live Score',
      type: 'polling',
      connected: !!hasScore,
    },
  ];
}

export default SourceStatus;
