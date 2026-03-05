import React from 'react';
import { NavLink, Outlet } from 'react-router-dom';

const TABS = [
  { to: '/', label: 'Live Matches' },
  { to: '/portfolio', label: 'Portfolio' },
  { to: '/analytics', label: 'Analytics' },
];

/** Tab navigation for the 3-layer structure + content outlet. */
const TabLayout: React.FC = () => {
  return (
    <div>
      <div
        style={{
          display: 'flex',
          gap: '0',
          borderBottom: '1px solid #334155',
          backgroundColor: '#1e293b',
          paddingLeft: '24px',
        }}
      >
        {TABS.map((tab) => (
          <NavLink
            key={tab.to}
            to={tab.to}
            end={tab.to === '/'}
            style={({ isActive }) => ({
              padding: '10px 20px',
              fontSize: '14px',
              fontWeight: 500,
              color: isActive ? '#3b82f6' : '#94a3b8',
              textDecoration: 'none',
              borderBottom: isActive ? '2px solid #3b82f6' : '2px solid transparent',
              transition: 'color 0.15s, border-color 0.15s',
            })}
          >
            {tab.label}
          </NavLink>
        ))}
      </div>
      <div style={{ padding: '24px' }}>
        <Outlet />
      </div>
    </div>
  );
};

export default TabLayout;
