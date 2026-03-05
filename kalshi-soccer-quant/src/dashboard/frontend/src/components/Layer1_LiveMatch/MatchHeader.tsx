import React from 'react';
import type { MatchState } from '../../types';
import { STATE_COLORS } from '../../utils/colors';
import { formatMinute, formatPlayerCount } from '../../utils/formatters';

interface MatchHeaderProps {
  state: MatchState;
}

/**
 * 1A: Match Status Header.
 *
 * Displays team names, score, match minute, engine phase, event state,
 * cooldown/ob_freeze/pricing status with color-coded borders and backgrounds.
 *
 * Color coding by state:
 *   IDLE + active       -> green border
 *   PRELIMINARY         -> yellow background (entire panel)
 *   COOLDOWN            -> blue border
 *   OB_FREEZE           -> red border
 *   HALFTIME            -> gray background
 *   FINISHED            -> dark background
 */
const MatchHeader: React.FC<MatchHeaderProps> = ({ state }) => {
  const stateKey = resolveStateKey(state);
  const colors = STATE_COLORS[stateKey] || STATE_COLORS.IDLE;

  return (
    <div
      style={{
        backgroundColor: colors.bg !== 'transparent' ? colors.bg : '#1e293b',
        border: `2px solid ${colors.border}`,
        borderRadius: '8px',
        padding: '12px 16px',
      }}
    >
      {/* Row 1: Teams + Time + Score */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <StatusDot color={colors.border} />
          <span style={{ fontSize: '16px', fontWeight: 700, color: '#f1f5f9' }}>
            {state.home} vs {state.away}
          </span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
          <span style={{ fontSize: '15px', color: '#94a3b8', fontFamily: 'monospace' }}>
            {formatMinute(state.t)}
          </span>
          <span style={{ fontSize: '18px', fontWeight: 800, color: '#f1f5f9', letterSpacing: '0.05em' }}>
            {state.score_h !== undefined ? `${state.score_h}-${state.score_a}` : '--'}
          </span>
        </div>
      </div>

      {/* Row 2: League | Engine Phase | Player Count | Event State */}
      <div style={{ marginTop: '6px', display: 'flex', gap: '10px', fontSize: '12px', color: '#94a3b8' }}>
        {state.engine_phase && (
          <Tag>{formatPhase(state.engine_phase)}</Tag>
        )}
        {state.X !== undefined && (
          <Tag>{formatPlayerCount(state.X)}</Tag>
        )}
        {state.event_state && (
          <Tag
            color={eventStateColor(state.event_state)}
            bold={state.event_state !== 'IDLE'}
          >
            {state.event_state}
          </Tag>
        )}
        {state.lifecycle && (
          <Tag>{state.lifecycle}</Tag>
        )}
      </div>

      {/* Row 3: Cooldown | OB Freeze | Pricing Mode */}
      <div style={{ marginTop: '4px', display: 'flex', gap: '14px', fontSize: '11px', color: '#64748b' }}>
        <StatusItem
          label="cooldown"
          active={state.cooldown === true}
          activeColor="#3b82f6"
        />
        <StatusItem
          label="ob_freeze"
          active={state.ob_freeze === true}
          activeColor="#ef4444"
        />
        {state.pricing_mode && (
          <span>pricing: {state.pricing_mode}</span>
        )}
        {state.trade_count !== undefined && (
          <span>trades: {state.trade_count}</span>
        )}
      </div>
    </div>
  );
};

/** Resolve which STATE_COLORS key to use based on state fields. */
function resolveStateKey(state: MatchState): string {
  // Priority: OB_FREEZE > COOLDOWN > event_state > engine_phase
  if (state.ob_freeze) return 'OB_FREEZE';
  if (state.cooldown) return 'COOLDOWN';

  if (state.event_state === 'PRELIMINARY') return 'PRELIMINARY';
  if (state.event_state === 'VAR_CANCELLED') return 'VAR_CANCELLED';

  if (state.engine_phase === 'HALFTIME' || state.engine_phase === 'halftime')
    return 'HALFTIME';
  if (state.engine_phase === 'FINISHED' || state.engine_phase === 'finished' || state.lifecycle === 'FINISHED')
    return 'FINISHED';

  return 'IDLE';
}

/** Format engine_phase for display (SECOND_HALF -> Second Half). */
function formatPhase(phase: string): string {
  return phase
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Map event_state to inline color. */
function eventStateColor(eventState: string): string {
  switch (eventState) {
    case 'PRELIMINARY': return '#eab308';
    case 'CONFIRMED': return '#22c55e';
    case 'VAR_CANCELLED': return '#ef4444';
    default: return '#94a3b8';
  }
}

/** Small colored dot indicating connection/state. */
const StatusDot: React.FC<{ color: string }> = ({ color }) => (
  <span
    style={{
      display: 'inline-block',
      width: '10px',
      height: '10px',
      borderRadius: '50%',
      backgroundColor: color,
      flexShrink: 0,
    }}
  />
);

/** Compact tag/pill for metadata display. */
const Tag: React.FC<{
  children: React.ReactNode;
  color?: string;
  bold?: boolean;
}> = ({ children, color, bold }) => (
  <span
    style={{
      padding: '1px 8px',
      borderRadius: '4px',
      backgroundColor: '#334155',
      color: color || '#94a3b8',
      fontWeight: bold ? 700 : 500,
      fontSize: '12px',
    }}
  >
    {children}
  </span>
);

/** Inline status indicator for cooldown/ob_freeze. */
const StatusItem: React.FC<{
  label: string;
  active: boolean;
  activeColor: string;
}> = ({ label, active, activeColor }) => (
  <span style={{ color: active ? activeColor : '#64748b', fontWeight: active ? 700 : 400 }}>
    {label}: {active ? 'ON' : 'OFF'}
  </span>
);

export default MatchHeader;
