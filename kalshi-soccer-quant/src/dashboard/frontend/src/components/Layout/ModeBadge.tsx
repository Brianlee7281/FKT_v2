import React from 'react';
import { MODE_COLORS } from '../../utils/colors';

interface ModeBadgeProps {
  mode: string;
}

/** Trading mode badge: PAPER (purple) or LIVE (green). */
const ModeBadge: React.FC<ModeBadgeProps> = ({ mode }) => {
  const colors = MODE_COLORS[mode] || MODE_COLORS.paper;
  return (
    <span
      style={{
        backgroundColor: colors.bg,
        color: colors.text,
        padding: '2px 10px',
        borderRadius: '4px',
        fontSize: '12px',
        fontWeight: 700,
        textTransform: 'uppercase',
        letterSpacing: '0.05em',
      }}
    >
      {mode} trading
    </span>
  );
};

export default ModeBadge;
