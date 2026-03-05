import React, { useEffect, useState } from 'react';
import type { ActiveMatch } from '../types';
import MatchPanel from '../components/Layer1_LiveMatch/MatchPanel';

const API_BASE = process.env.REACT_APP_API_URL || 'http://localhost:8000';

/**
 * Layer 1: Live Matches — 2×2 grid of active match panels.
 *
 * D2.5 features:
 *   - Click panel → expand to full width (focus mode)
 *   - Click again or press Esc → return to grid
 *   - HALFTIME panels auto-minimize to header only
 */
const LiveMatchesPage: React.FC = () => {
  const [matches, setMatches] = useState<ActiveMatch[]>([]);
  const [focusedId, setFocusedId] = useState<string | null>(null);

  useEffect(() => {
    const fetchMatches = async () => {
      try {
        const resp = await fetch(`${API_BASE}/api/matches/active`);
        if (resp.ok) setMatches(await resp.json());
      } catch {
        // Retry on next poll
      }
    };

    fetchMatches();
    const interval = setInterval(fetchMatches, 5000);
    return () => clearInterval(interval);
  }, []);

  // Esc key exits focus mode
  useEffect(() => {
    if (!focusedId) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setFocusedId(null);
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [focusedId]);

  if (matches.length === 0) {
    return (
      <div style={{ textAlign: 'center', padding: '60px 20px', color: '#94a3b8' }}>
        <h2 style={{ fontSize: '20px', fontWeight: 500, marginBottom: '8px' }}>
          No Active Matches
        </h2>
        <p style={{ fontSize: '14px' }}>
          Matches will appear here when the scheduler starts engines.
        </p>
      </div>
    );
  }

  const handlePanelClick = (matchId: string) => {
    setFocusedId((prev) => (prev === matchId ? null : matchId));
  };

  // In focus mode, show only the focused panel at full width
  if (focusedId) {
    const focusedMatch = matches.find((m) => m.match_id === focusedId);
    if (!focusedMatch) {
      setFocusedId(null);
      return null;
    }
    return (
      <div>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
          <h2 style={{ fontSize: '18px', fontWeight: 600 }}>
            Live Matches ({matches.length}) — Focus Mode
          </h2>
          <button
            onClick={() => setFocusedId(null)}
            style={{
              padding: '4px 12px',
              fontSize: '12px',
              color: '#94a3b8',
              backgroundColor: '#334155',
              border: '1px solid #475569',
              borderRadius: '4px',
              cursor: 'pointer',
            }}
          >
            Back to Grid (Esc)
          </button>
        </div>
        <MatchPanel
          match={focusedMatch}
          focused
          onClick={() => setFocusedId(null)}
        />
      </div>
    );
  }

  return (
    <div>
      <h2 style={{ fontSize: '18px', fontWeight: 600, marginBottom: '16px' }}>
        Live Matches ({matches.length})
      </h2>
      <div style={{
        display: 'grid',
        gap: '12px',
        gridTemplateColumns: 'repeat(auto-fill, minmax(480px, 1fr))',
      }}>
        {matches.map((m) => (
          <MatchPanel
            key={m.match_id}
            match={m}
            onClick={() => handlePanelClick(m.match_id)}
          />
        ))}
      </div>
    </div>
  );
};

export default LiveMatchesPage;
