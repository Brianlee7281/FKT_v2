import React, { useEffect, useState } from 'react';
import { BrowserRouter, Routes, Route } from 'react-router-dom';
import Navbar from './components/Layout/Navbar';
import TabLayout from './components/Layout/TabLayout';
import LiveMatchesPage from './pages/LiveMatchesPage';
import PortfolioPage from './pages/PortfolioPage';
import AnalyticsPage from './pages/AnalyticsPage';

const API_BASE = process.env.REACT_APP_API_URL || 'http://localhost:8000';

const App: React.FC = () => {
  const [mode, setMode] = useState('paper');
  const [connected, setConnected] = useState(false);

  // Fetch trading mode from portfolio summary
  useEffect(() => {
    const fetchMode = async () => {
      try {
        const resp = await fetch(`${API_BASE}/api/portfolio/summary`);
        if (resp.ok) {
          const data = await resp.json();
          setMode(data.trading_mode || 'paper');
          setConnected(true);
        }
      } catch {
        setConnected(false);
      }
    };

    fetchMode();
    const interval = setInterval(fetchMode, 10000);
    return () => clearInterval(interval);
  }, []);

  return (
    <BrowserRouter>
      <div style={{ minHeight: '100vh', backgroundColor: '#0f172a', color: '#e2e8f0' }}>
        <Navbar mode={mode} connected={connected} />
        <Routes>
          <Route element={<TabLayout />}>
            <Route path="/" element={<LiveMatchesPage />} />
            <Route path="/portfolio" element={<PortfolioPage />} />
            <Route path="/analytics" element={<AnalyticsPage />} />
          </Route>
        </Routes>
      </div>
    </BrowserRouter>
  );
};

export default App;
