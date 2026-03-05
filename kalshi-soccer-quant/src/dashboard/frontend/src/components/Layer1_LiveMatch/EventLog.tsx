import React, { useEffect, useRef, useState } from 'react';
import type { MatchState } from '../../types';
import { EVENT_COLORS } from '../../utils/colors';
import { formatMinute } from '../../utils/formatters';

/** Internal event entry accumulated from state transitions. */
interface LogEntry {
  id: number;
  t: number;
  type: string;
  message: string;
}

interface EventLogProps {
  state: MatchState | null;
}

const MAX_ENTRIES = 200;
let nextId = 0;

/**
 * 1E: Event Log — real-time stream of match events.
 *
 * Derives events from state snapshot transitions (event_state, cooldown,
 * ob_freeze changes). Auto-scrolls to bottom. TICK hidden by default.
 *
 * Event type color coding:
 *   PRELIMINARY   -> yellow bg
 *   CONFIRMED     -> green bg
 *   VAR_CANCELLED -> red text
 *   OB_FREEZE     -> red bg
 *   COOLDOWN      -> blue bg
 *   SIGNAL        -> purple text
 *   ORDER         -> bold text
 *   TICK          -> light gray (hidden by default)
 */
const EventLog: React.FC<EventLogProps> = ({ state }) => {
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [showTick, setShowTick] = useState(false);
  const [autoScroll, setAutoScroll] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);
  const prevState = useRef<{
    event_state?: string;
    cooldown?: boolean;
    ob_freeze?: boolean;
    score_h?: number;
    score_a?: number;
    engine_phase?: string;
    t?: number;
  }>({});

  // Derive events from state transitions
  useEffect(() => {
    if (!state || state.t === undefined) return;

    const prev = prevState.current;
    const newEntries: LogEntry[] = [];

    // Event state transitions
    if (state.event_state && state.event_state !== prev.event_state) {
      if (state.event_state === 'PRELIMINARY') {
        const scoreStr = state.score_h !== undefined
          ? `score ${prev.score_h ?? '?'}-${prev.score_a ?? '?'} -> ${state.score_h}-${state.score_a}`
          : 'event detected';
        newEntries.push(makeEntry(state.t, 'PRELIMINARY', scoreStr));
      } else if (state.event_state === 'CONFIRMED') {
        const scoreStr = state.score_h !== undefined
          ? `S=${state.score_h}-${state.score_a}`
          : '';
        newEntries.push(makeEntry(state.t, 'CONFIRMED', `Event confirmed. ${scoreStr}`));
      } else if (state.event_state === 'VAR_CANCELLED') {
        newEntries.push(makeEntry(state.t, 'VAR_CANCELLED', 'VAR cancellation — state rollback'));
      }
    }

    // Cooldown transitions
    if (state.cooldown !== prev.cooldown) {
      if (state.cooldown) {
        newEntries.push(makeEntry(state.t, 'COOLDOWN', 'Cooldown started'));
      } else if (prev.cooldown === true) {
        newEntries.push(makeEntry(state.t, 'COOLDOWN', 'Cooldown ended'));
      }
    }

    // OB Freeze transitions
    if (state.ob_freeze !== prev.ob_freeze) {
      if (state.ob_freeze) {
        newEntries.push(makeEntry(state.t, 'OB_FREEZE', 'Orderbook freeze triggered'));
      } else if (prev.ob_freeze === true) {
        newEntries.push(makeEntry(state.t, 'OB_FREEZE', 'Orderbook freeze lifted'));
      }
    }

    // Engine phase transitions
    if (state.engine_phase && state.engine_phase !== prev.engine_phase) {
      const phase = state.engine_phase.replace(/_/g, ' ');
      newEntries.push(makeEntry(state.t, 'TICK', `Phase: ${phase}`));
    }

    // Generate TICK entries for regular snapshots (throttled: every ~5 minutes of match time)
    if (prev.t !== undefined && state.t - prev.t >= 5 && newEntries.length === 0) {
      const parts: string[] = [];
      if (state.mu_H !== undefined) parts.push(`mu_H=${state.mu_H.toFixed(2)}`);
      if (state.mu_A !== undefined) parts.push(`mu_A=${state.mu_A.toFixed(2)}`);
      if (state.P_true) {
        const firstMarket = Object.keys(state.P_true)[0];
        if (firstMarket) {
          parts.push(`P(${firstMarket})=${state.P_true[firstMarket].toFixed(3)}`);
        }
      }
      if (parts.length > 0) {
        newEntries.push(makeEntry(state.t, 'TICK', parts.join(' ')));
      }
    }

    if (newEntries.length > 0) {
      setEntries((prev) => {
        const combined = [...prev, ...newEntries];
        return combined.length > MAX_ENTRIES ? combined.slice(-MAX_ENTRIES) : combined;
      });
    }

    // Update prev state
    prevState.current = {
      event_state: state.event_state,
      cooldown: state.cooldown,
      ob_freeze: state.ob_freeze,
      score_h: state.score_h,
      score_a: state.score_a,
      engine_phase: state.engine_phase,
      t: state.t,
    };
  }, [state]);

  // Auto-scroll to bottom
  useEffect(() => {
    if (autoScroll && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [entries, autoScroll]);

  const visibleEntries = showTick ? entries : entries.filter((e) => e.type !== 'TICK');

  return (
    <div style={{ marginTop: '8px' }}>
      {/* Header bar */}
      <div style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        marginBottom: '4px',
      }}>
        <span style={{ fontSize: '13px', fontWeight: 600, color: '#94a3b8' }}>
          Event Log ({visibleEntries.length})
        </span>
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <label style={{ fontSize: '11px', color: '#64748b', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '4px' }}>
            <input
              type="checkbox"
              checked={showTick}
              onChange={(e) => setShowTick(e.target.checked)}
              style={{ width: '12px', height: '12px' }}
            />
            Show TICK
          </label>
          <label style={{ fontSize: '11px', color: '#64748b', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '4px' }}>
            <input
              type="checkbox"
              checked={autoScroll}
              onChange={(e) => setAutoScroll(e.target.checked)}
              style={{ width: '12px', height: '12px' }}
            />
            Auto-scroll
          </label>
        </div>
      </div>

      {/* Log entries */}
      <div
        ref={scrollRef}
        style={{
          backgroundColor: '#0f172a',
          border: '1px solid #334155',
          borderRadius: '6px',
          maxHeight: '200px',
          overflowY: 'auto',
          padding: '4px 0',
          fontSize: '12px',
          fontFamily: 'monospace',
        }}
      >
        {visibleEntries.length === 0 ? (
          <div style={{ padding: '16px', textAlign: 'center', color: '#64748b' }}>
            No events yet. Events will appear as the match progresses.
          </div>
        ) : (
          visibleEntries.map((entry) => {
            const colors = EVENT_COLORS[entry.type] || EVENT_COLORS.TICK;
            return (
              <div
                key={entry.id}
                style={{
                  padding: '2px 10px',
                  backgroundColor: colors.bg !== 'transparent' ? colors.bg + '20' : 'transparent',
                  color: colors.text,
                  display: 'flex',
                  gap: '8px',
                  lineHeight: '1.6',
                }}
              >
                <span style={{ color: '#64748b', flexShrink: 0, width: '42px' }}>
                  {formatMinute(entry.t)}
                </span>
                <span style={{
                  flexShrink: 0,
                  width: '90px',
                  fontWeight: entry.type === 'ORDER' ? 700 : 600,
                  color: colors.text,
                }}>
                  {entry.type}
                </span>
                <span style={{ color: entry.type === 'TICK' ? '#64748b' : '#cbd5e1' }}>
                  {entry.message}
                </span>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
};

function makeEntry(t: number, type: string, message: string): LogEntry {
  return { id: nextId++, t, type, message };
}

export default EventLog;
