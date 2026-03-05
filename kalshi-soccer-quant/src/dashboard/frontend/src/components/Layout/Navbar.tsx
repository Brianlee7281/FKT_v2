import React from 'react';
import ModeBadge from './ModeBadge';

interface NavbarProps {
  mode: string;
  connected: boolean;
}

/** Top navigation bar with system title, mode badge, and connection status. */
const Navbar: React.FC<NavbarProps> = ({ mode, connected }) => {
  return (
    <nav
      style={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        padding: '12px 24px',
        backgroundColor: '#1e293b',
        borderBottom: '1px solid #334155',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
        <h1
          style={{
            margin: 0,
            fontSize: '18px',
            fontWeight: 600,
            color: '#f1f5f9',
          }}
        >
          Kalshi Soccer Quant
        </h1>
        <ModeBadge mode={mode} />
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
        <span
          style={{
            width: '8px',
            height: '8px',
            borderRadius: '50%',
            backgroundColor: connected ? '#22c55e' : '#ef4444',
          }}
        />
        <span style={{ fontSize: '13px', color: '#94a3b8' }}>
          {connected ? 'Connected' : 'Disconnected'}
        </span>
      </div>
    </nav>
  );
};

export default Navbar;
