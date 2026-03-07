import React, { useEffect, useMemo, useState } from 'react';
import type { Position } from '../../types';
import { formatCents, formatPnl } from '../../utils/formatters';

const API_BASE = process.env.REACT_APP_API_URL || '';

type SortKey = 'match' | 'market' | 'pnl';
type FilterMode = 'all' | 'open' | 'settled';

const MARKET_LABELS: Record<string, string> = {
  over_25: 'Over 2.5',
  home_win: 'Home Win',
  draw: 'Draw',
  away_win: 'Away Win',
  btts: 'BTTS',
};

/**
 * 2B: Position Table — all positions across matches.
 *
 * Table: Match | Market | Dir | Entry | Current | P&L | bet365 | Status
 *
 * Row colors:
 *   Unrealized profit -> light green bg
 *   Unrealized loss   -> light red bg
 *   Settled win       -> green text
 *   Settled loss      -> red text
 *
 * Sortable by: Match / P&L / Market
 * Filterable: Active only / Settled only / All
 *
 * Data: REST /api/portfolio/positions + real-time updates from WebSocket state.
 */
const PositionTable: React.FC = () => {
  const [positions, setPositions] = useState<Position[]>([]);
  const [sortKey, setSortKey] = useState<SortKey>('match');
  const [filter, setFilter] = useState<FilterMode>('all');

  // Fetch positions from REST endpoint
  useEffect(() => {
    const fetchPositions = async () => {
      try {
        const resp = await fetch(`${API_BASE}/api/portfolio/positions`);
        if (resp.ok) setPositions(await resp.json());
      } catch {
        // Retry on next poll
      }
    };

    fetchPositions();
    const interval = setInterval(fetchPositions, 5000);
    return () => clearInterval(interval);
  }, []);

  // Filter
  const filtered = useMemo(() => {
    if (filter === 'all') return positions;
    if (filter === 'open') return positions.filter((p) => p.status === 'open');
    return positions.filter((p) => p.status !== 'open');
  }, [positions, filter]);

  // Sort
  const sorted = useMemo(() => {
    const arr = [...filtered];
    switch (sortKey) {
      case 'match':
        arr.sort((a, b) => `${a.home}${a.away}`.localeCompare(`${b.home}${b.away}`));
        break;
      case 'market':
        arr.sort((a, b) => a.market.localeCompare(b.market));
        break;
      case 'pnl':
        arr.sort((a, b) => (b.pnl ?? 0) - (a.pnl ?? 0));
        break;
    }
    return arr;
  }, [filtered, sortKey]);

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
        <span style={{ fontSize: '14px', fontWeight: 600, color: '#e2e8f0' }}>
          Positions ({filtered.length})
        </span>

        <div style={{ display: 'flex', gap: '6px', alignItems: 'center' }}>
          {/* Filter tabs */}
          {(['all', 'open', 'settled'] as FilterMode[]).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              style={{
                padding: '3px 10px',
                fontSize: '11px',
                fontWeight: filter === f ? 700 : 400,
                color: filter === f ? '#3b82f6' : '#94a3b8',
                backgroundColor: filter === f ? '#1e3a5f' : '#1e293b',
                border: '1px solid #334155',
                borderRadius: '4px',
                cursor: 'pointer',
                textTransform: 'capitalize',
              }}
            >
              {f}
            </button>
          ))}
        </div>
      </div>

      <div style={{
        backgroundColor: '#0f172a',
        border: '1px solid #334155',
        borderRadius: '6px',
        overflow: 'auto',
      }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '12px', minWidth: '600px' }}>
          <thead>
            <tr style={{ borderBottom: '1px solid #334155' }}>
              <SortTh active={sortKey === 'match'} onClick={() => setSortKey('match')}>Match</SortTh>
              <SortTh active={sortKey === 'market'} onClick={() => setSortKey('market')}>Market</SortTh>
              <Th>Dir</Th>
              <Th align="right">Entry</Th>
              <Th align="right">Current</Th>
              <SortTh active={sortKey === 'pnl'} onClick={() => setSortKey('pnl')} align="right">P&L</SortTh>
              <Th>bet365</Th>
              <Th>Status</Th>
            </tr>
          </thead>
          <tbody>
            {sorted.length === 0 ? (
              <tr>
                <td colSpan={8} style={{ padding: '20px', textAlign: 'center', color: '#64748b' }}>
                  No positions to display.
                </td>
              </tr>
            ) : (
              sorted.map((p, i) => {
                const rowBg = getRowBg(p);
                return (
                  <tr key={`${p.match_id}-${p.market}-${i}`} style={{ backgroundColor: rowBg, borderBottom: '1px solid #1e293b' }}>
                    <Td>
                      <span style={{ fontWeight: 600 }}>{p.home}</span>
                      <span style={{ color: '#64748b' }}> vs </span>
                      <span style={{ fontWeight: 600 }}>{p.away}</span>
                    </Td>
                    <Td>{MARKET_LABELS[p.market] || p.market}</Td>
                    <Td>
                      <span style={{ color: p.direction === 'BUY_YES' ? '#22c55e' : '#ef4444', fontWeight: 600 }}>
                        {p.direction}
                      </span>
                    </Td>
                    <Td align="right">{formatCents(p.entry_price)}</Td>
                    <Td align="right">{p.current_price !== undefined ? formatCents(p.current_price) : '--'}</Td>
                    <Td align="right">
                      <span style={{ color: (p.pnl ?? 0) >= 0 ? '#22c55e' : '#ef4444', fontWeight: 600 }}>
                        {p.pnl !== undefined ? formatPnl(p.pnl) : '--'}
                      </span>
                    </Td>
                    <Td>
                      {p.bet365_aligned === undefined ? (
                        <span style={{ color: '#64748b' }}>--</span>
                      ) : p.bet365_aligned ? (
                        <span style={{ color: '#22c55e' }}>OK</span>
                      ) : (
                        <span style={{ color: '#eab308', fontWeight: 700 }}>WARN</span>
                      )}
                    </Td>
                    <Td>
                      <StatusBadge status={p.status} />
                    </Td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
};

function getRowBg(p: Position): string {
  if (p.status === 'open') {
    if (p.pnl !== undefined && p.pnl > 0) return 'rgba(34, 197, 94, 0.06)';
    if (p.pnl !== undefined && p.pnl < 0) return 'rgba(239, 68, 68, 0.06)';
  }
  return 'transparent';
}

const StatusBadge: React.FC<{ status: string }> = ({ status }) => {
  let color = '#94a3b8';
  let label = status;

  if (status === 'open') {
    color = '#3b82f6';
    label = 'OPEN';
  } else if (status === 'settled_win') {
    color = '#22c55e';
    label = 'WIN';
  } else if (status === 'settled_loss') {
    color = '#ef4444';
    label = 'LOSS';
  }

  return (
    <span style={{
      padding: '1px 6px',
      borderRadius: '3px',
      backgroundColor: color + '20',
      color,
      fontWeight: 600,
      fontSize: '10px',
      textTransform: 'uppercase',
    }}>
      {label}
    </span>
  );
};

const Th: React.FC<{ children: React.ReactNode; align?: 'left' | 'right' }> = ({ children, align = 'left' }) => (
  <th style={{
    padding: '8px 10px',
    textAlign: align,
    color: '#64748b',
    fontWeight: 600,
    fontSize: '11px',
    textTransform: 'uppercase',
    letterSpacing: '0.05em',
    whiteSpace: 'nowrap',
  }}>
    {children}
  </th>
);

const SortTh: React.FC<{
  children: React.ReactNode;
  active: boolean;
  onClick: () => void;
  align?: 'left' | 'right';
}> = ({ children, active, onClick, align = 'left' }) => (
  <th
    onClick={onClick}
    style={{
      padding: '8px 10px',
      textAlign: align,
      color: active ? '#3b82f6' : '#64748b',
      fontWeight: 600,
      fontSize: '11px',
      textTransform: 'uppercase',
      letterSpacing: '0.05em',
      cursor: 'pointer',
      whiteSpace: 'nowrap',
      userSelect: 'none',
    }}
  >
    {children} {active ? '▼' : ''}
  </th>
);

const Td: React.FC<{ children: React.ReactNode; align?: 'left' | 'right' }> = ({ children, align = 'left' }) => (
  <td style={{
    padding: '6px 10px',
    textAlign: align,
    color: '#cbd5e1',
    whiteSpace: 'nowrap',
  }}>
    {children}
  </td>
);

export default PositionTable;
