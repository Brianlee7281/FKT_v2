# Dashboard Implementation Roadmap

## Starting Point

Steps 5.1–5.3 of the main roadmap are complete:

```
✅ 5.1: MatchEngine — full lifecycle orchestrator working
✅ 5.2: Scheduler — 24/7 auto-scan, spawn, monitor, cleanup
✅ 5.3: Alert Service — Redis subscriber → Slack/Telegram dispatch
```

The trading engine runs headless. Data flows through Redis pub/sub.
Trade logs accumulate in PostgreSQL. Alerts fire to Slack.

**What's missing:** You can't see what's happening. This roadmap builds
the visual layer — from a single chart to a full analytics platform.

---

## Architecture Recap

```
Trading Engine (already running)
    │
    ├── Redis Pub/Sub channels:
    │   ├── match:{id}:state      (every 1s per match)
    │   ├── match:{id}:events     (on events)
    │   └── alerts                (on alerts)
    │
    └── PostgreSQL tables:
        ├── trade_logs            (every trade)
        ├── positions             (open + settled)
        ├── tick_snapshots        (every 1s, hypertable)
        ├── event_logs            (every event, hypertable)
        ├── daily_analytics       (daily aggregation)
        └── match_jobs            (schedule + status)

Dashboard (what we're building)
    │
    ├── Backend: FastAPI + WebSocket
    │   ├── /ws/live/{match_id}   → subscribes to Redis match channel
    │   ├── /ws/portfolio         → subscribes to all match channels
    │   └── /api/analytics/*      → queries PostgreSQL
    │
    └── Frontend: React + Recharts
        ├── Layer 1: Live Match    (real-time, per match)
        ├── Layer 2: Portfolio     (real-time, all matches)
        └── Layer 3: Analytics     (historical, post-match)
```

---

## Sprint D1: Backend + Skeleton (3–4 days)

**Goal:** FastAPI server boots, WebSocket streams data, React renders a blank shell.

### D1.1: FastAPI Server

```
src/dashboard/
├── __init__.py
├── server.py              ← main app
└── api/
    ├── __init__.py
    ├── live.py            ← WebSocket endpoints
    ├── portfolio.py       ← Portfolio REST + WS
    └── analytics.py       ← Analytics REST endpoints
```

```python
# src/dashboard/server.py
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Kalshi Soccer Quant Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # React dev server
    allow_methods=["*"],
    allow_headers=["*"],
)

# WebSocket: single match stream
@app.websocket("/ws/live/{match_id}")
async def ws_match(websocket, match_id: str):
    await websocket.accept()
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"match:{match_id}:state")
    try:
        async for msg in pubsub.listen():
            if msg["type"] == "message":
                await websocket.send_text(msg["data"])
    finally:
        await pubsub.unsubscribe()

# WebSocket: all matches (portfolio view)
@app.websocket("/ws/portfolio")
async def ws_portfolio(websocket):
    await websocket.accept()
    pubsub = redis.pubsub()
    await pubsub.psubscribe("match:*:state")
    try:
        async for msg in pubsub.listen():
            if msg["type"] == "pmessage":
                await websocket.send_text(msg["data"])
    finally:
        await pubsub.unsubscribe()

# REST: active matches list
@app.get("/api/matches/active")
async def get_active_matches():
    return await db.query("SELECT * FROM match_jobs WHERE status = 'LIVE'")

# Serve React build in production
# app.mount("/", StaticFiles(directory="src/dashboard/frontend/build", html=True))
```

### D1.2: React Scaffold

```bash
cd src/dashboard/frontend
npx create-react-app . --template typescript
npm install recharts react-router-dom
```

```
src/dashboard/frontend/src/
├── App.tsx
├── index.tsx
├── components/
│   └── Layout/
│       ├── Navbar.tsx
│       ├── ModeBadge.tsx        # PAPER (purple) / LIVE (default)
│       └── TabLayout.tsx        # [Live Matches] [Portfolio] [Analytics]
├── hooks/
│   ├── useMatchStream.ts        # WebSocket → match state
│   ├── usePortfolio.ts          # WebSocket → portfolio aggregate
│   └── useAnalytics.ts          # REST → analytics data
├── pages/
│   ├── LiveMatchesPage.tsx
│   ├── PortfolioPage.tsx
│   └── AnalyticsPage.tsx
└── utils/
    ├── formatters.ts            # price/P&L formatting
    └── colors.ts                # state → color mapping
```

### D1.3: Core Hook — useMatchStream

```typescript
// src/hooks/useMatchStream.ts
export function useMatchStream(matchId: string) {
  const [state, setState] = useState<MatchState | null>(null);

  useEffect(() => {
    const ws = new WebSocket(`ws://localhost:8000/ws/live/${matchId}`);
    ws.onmessage = (event) => {
      setState(JSON.parse(event.data));
    };
    ws.onclose = () => {
      // Auto-reconnect after 2s
      setTimeout(() => ws.close(), 2000);
    };
    return () => ws.close();
  }, [matchId]);

  return state;
}
```

### Verification

- `uvicorn src.dashboard.server:app --reload` starts on port 8000
- `npm start` in frontend/ starts on port 3000
- Open browser → blank shell with navbar + tabs renders
- WebSocket connects (verify in browser dev tools Network tab)
- If a match engine is running, state snapshots appear in console

---

## Sprint D2: Layer 1 Core — The Chart That Matters (4–5 days)

**Goal:** The single most important visualization works: P_true vs P_kalshi vs P_bet365 in real time.

### D2.1: MatchHeader (1A)

```
┌─────────────────────────────────────────────────────────────┐
│  🟢 Arsenal vs Chelsea              67:23    ⚽ 1-1          │
│  EPL  │  SECOND_HALF  │  11v11  │  IDLE                    │
│  cooldown: OFF  │  ob_freeze: OFF  │  pricing: ANALYTICAL   │
└─────────────────────────────────────────────────────────────┘
```

```typescript
// src/components/Layer1_LiveMatch/MatchHeader.tsx

interface MatchHeaderProps {
  state: MatchState;
}

// Color mapping:
// IDLE + active       → green border
// PRELIMINARY         → yellow background (entire panel)
// COOLDOWN            → blue border
// OB_FREEZE           → red border
// HALFTIME            → gray background
// FINISHED            → dark background
```

**Data consumed from state snapshot:**

| Field | Display |
|-------|---------|
| `state.score` | "1-1" |
| `state.t` | "67:23" (convert minutes to mm:ss) |
| `state.engine_phase` | "SECOND_HALF" |
| `state.X` | "11v11" / "10v11" / "11v10" / "10v10" |
| `state.event_state` | "IDLE" / "PRELIMINARY" / "CONFIRMED" |
| `state.cooldown` | "OFF" / "ON (12s remaining)" |
| `state.ob_freeze` | "OFF" / "ON" |
| `state.pricing_mode` | "ANALYTICAL" / "MONTE_CARLO" |

### D2.2: PriceChart (1B) ⭐ — The Core Visualization

This is the single chart that answers "Is the model working?"

```typescript
// src/components/Layer1_LiveMatch/PriceChart.tsx

// Uses Recharts LineChart with 3 lines:
// 1. P_true      → blue solid (thick)
// 2. P_kalshi    → red solid (mid = (bid+ask)/2)
// 3. P_bet365    → green dashed

// Additional visual elements:
// - Kalshi bid-ask spread → red shaded band
// - Edge regions → light blue fill where P_true > P_kalshi_ask
// - Goal markers → vertical line + ⚽ icon
//   - PRELIMINARY: dashed yellow line
//   - CONFIRMED: solid green line
//   - VAR_CANCELLED: red X marker
// - Red card markers → vertical line + 🟥
// - Halftime → gray shaded region
// - Trade markers → 🔼 (entry) 🔽 (exit) on the P_kalshi line
```

**Implementation approach:**

```typescript
// State accumulator — build time series from WebSocket snapshots
const [history, setHistory] = useState<PricePoint[]>([]);

useEffect(() => {
  if (state) {
    setHistory(prev => [...prev, {
      t: state.t,
      P_true: state.P_true[selectedMarket],
      P_kalshi_mid: (state.P_kalshi_bid + state.P_kalshi_ask) / 2,
      P_kalshi_bid: state.P_kalshi_bid,
      P_kalshi_ask: state.P_kalshi_ask,
      P_bet365: state.P_bet365[selectedMarket],
      event_state: state.event_state,
    }]);
  }
}, [state]);
```

**Market tab selector:** Over 2.5 | Home Win | Draw | Away Win | BTTS

Each tab switches `selectedMarket`, filtering P_true/P_bet365 to that market.

**Event overlay data:** Fetched from `event_logs` table or accumulated from the events Redis channel.

### D2.3: EventLog (1E)

```
┌──────────────────────────────────────────────────┐
│  Event Log                          [Auto-scroll] │
│                                                    │
│  67:23  TICK    μ_H=0.42 μ_A=0.38 P(O2.5)=0.58  │
│  65:01  ORDER   BUY YES Over2.5 @45¢ ×15         │
│  65:00  CONFIRMED  Goal (Away). S=1-1, ΔS=0      │
│  64:55  PRELIMINARY  score 1-0→1-1 (Live Odds)   │
│  64:54  OB_FREEZE  bet365 Δodds=15.2%            │
└──────────────────────────────────────────────────┘
```

```typescript
// Event type → color mapping
const EVENT_COLORS = {
  PRELIMINARY:    { bg: '#fef3c7', text: '#92400e' },  // yellow
  CONFIRMED:      { bg: '#d1fae5', text: '#065f46' },  // green
  VAR_CANCELLED:  { bg: '#fee2e2', text: '#991b1b' },  // red text
  OB_FREEZE:      { bg: '#fee2e2', text: '#991b1b' },  // red bg
  COOLDOWN:       { bg: '#dbeafe', text: '#1e40af' },  // blue
  SIGNAL:         { bg: '#ede9fe', text: '#5b21b6' },  // purple
  ORDER:          { bg: '#f3f4f6', text: '#111827' },  // bold gray
  TICK:           { bg: '#f9fafb', text: '#9ca3af' },  // light gray
};

// Filter toggle: hide TICK events by default (too noisy)
```

**Data source:** Subscribe to `match:{id}:events` Redis channel, or derive from state snapshots.

### D2.4: MatchPanel Container

```typescript
// src/components/Layer1_LiveMatch/MatchPanel.tsx

// Assembles 1A + 1B + 1E into a single match panel
// Props: matchId: string

export function MatchPanel({ matchId }: { matchId: string }) {
  const state = useMatchStream(matchId);
  if (!state) return <Loading />;

  return (
    <div className={panelBorderColor(state.event_state)}>
      <MatchHeader state={state} />
      <PriceChart state={state} matchId={matchId} />
      <EventLog matchId={matchId} />
    </div>
  );
}
```

### D2.5: LiveMatchesPage — Grid Layout

```typescript
// src/pages/LiveMatchesPage.tsx

// Fetch active matches from /api/matches/active
// Render MatchPanel for each in a 2×2 grid
// Click a panel → expand to full width (focus mode)
// HALFTIME panels auto-minimize to header only
```

### Verification

- Start a match engine (paper mode)
- Open dashboard → Live Matches tab
- See the match panel with real-time header updates
- **PriceChart shows 3 lines updating every second**
- Goal event appears as PRELIMINARY (yellow dashed) → CONFIRMED (green solid)
- EventLog scrolls with color-coded entries
- Market tabs switch between Over 2.5 / Home Win / etc.

---

## Sprint D3: Layer 1 Remaining + Layer 2 Core (4–5 days)

**Goal:** Complete the live match view and add portfolio-level monitoring.

### D3.1: MuChart (1C)

```typescript
// src/components/Layer1_LiveMatch/MuChart.tsx

// Small chart showing μ_H and μ_A decay over time
// Data: state.mu_H, state.mu_A from each tick
// Visual: two lines (blue=home, red=away) declining over time
// Jump markers when goals or red cards cause step changes
```

This chart answers "Is the model processing events correctly?"
If μ doesn't jump after a goal, something is broken.

### D3.2: SignalPanel (1D)

```typescript
// src/components/Layer1_LiveMatch/SignalPanel.tsx

// Two sections:
// 1. Active Signals — current tick's signal for each market
//    Market | Direction | EV | Alignment | Suggested Qty
//
// 2. Open Positions — positions for this match
//    Market | Dir | Entry | Current | P&L | bet365 ✓/⚠

// bet365 indicator:
//   ✓ (green) = bet365 aligns with position direction
//   ⚠ (yellow) = bet365 divergence alert triggered
```

### D3.3: SourceStatus (1F)

```typescript
// src/components/Layer1_LiveMatch/SourceStatus.tsx

// Compact status bar showing 3-Layer health:
//   Live Odds WS:  🟢 Connected  <1s
//   Kalshi WS:     🟢 Connected  ~1s
//   Live Score:    🟢 Polling    3s cycle
//
// Status logic:
//   🟢 = last message < 5s ago
//   🟡 = last message 5-10s ago
//   🔴 = last message > 10s ago or error
```

### D3.4: RiskDashboard (2A)

```typescript
// src/components/Layer2_Portfolio/RiskDashboard.tsx

// Visual progress bars for 3-Layer risk limits:
//   L1 (Order 3%):   ████░░░░░░  $87 / $150
//   L2 (Match 5%):   per-match bars
//   L3 (Total 20%):  ████░░░░░░  $412 / $1,000
//
// Color: green (<50%), yellow (50-80%), red (>80%)
//
// Summary metrics:
//   Bankroll | Active Matches | Open Positions | Exposure | Unrealized P&L
```

**Data source:** Aggregate all match states from `/ws/portfolio` WebSocket.

### D3.5: PositionTable (2B)

```typescript
// src/components/Layer2_Portfolio/PositionTable.tsx

// Table: Match | Market | Dir | Entry | Current | P&L | bet365 | Status
//
// Row colors:
//   Unrealized profit → light green bg
//   Unrealized loss   → light red bg
//   Settled win       → green text + ✅
//   Settled loss      → red text + ❌
//
// Sortable by: Match / P&L / Market
// Filterable: Active only / Settled only / All
```

**Data source:** Combine real-time positions from WebSocket + settled positions from REST `/api/positions`.

### D3.6: PnLTimeline (2C)

```typescript
// src/components/Layer2_Portfolio/PnLTimeline.tsx

// Two lines:
//   Realized P&L (solid, step function — jumps on settlement)
//   Total P&L (solid, continuous — realized + unrealized)
//
// X-axis: time of day (UTC)
// Y-axis: dollar P&L
```

**Data source:** REST `/api/portfolio/pnl_timeline` for historical + WebSocket for live updates.

### D3.7: PortfolioPage Assembly

```typescript
// src/pages/PortfolioPage.tsx

export function PortfolioPage() {
  const portfolio = usePortfolio();
  return (
    <>
      <RiskDashboard portfolio={portfolio} />
      <PositionTable positions={portfolio.positions} />
      <PnLTimeline data={portfolio.pnlHistory} />
    </>
  );
}
```

### Backend Endpoints Needed

```python
# src/dashboard/api/portfolio.py

@app.get("/api/positions")
async def get_positions(status: str = "all"):
    """All positions, filterable by open/settled/all."""
    ...

@app.get("/api/portfolio/summary")
async def get_portfolio_summary():
    """Bankroll, exposure, unrealized P&L, risk limit usage."""
    ...

@app.get("/api/portfolio/pnl_timeline")
async def get_pnl_timeline(date: str = "today"):
    """Timestamped P&L series for chart."""
    ...
```

### Verification

- All 6 Layer 1 components render correctly in match panel
- μ chart shows jump when a goal occurs during replay
- SourceStatus shows green when all 3 sources are connected
- Portfolio page shows aggregated positions across multiple matches
- Risk dashboard progress bars update when new positions are opened
- P&L timeline updates in real-time during active matches

---

## Sprint D4: Layer 3 — Analytics Core (4–5 days)

**Goal:** Post-match analytics and system health monitoring.

### D4.1: HealthDashboard (3A)

```typescript
// src/components/Layer3_Analytics/HealthDashboard.tsx

// 7 gauge indicators with traffic light colors:
//
// Metric                  Value    Status
// Brier Score             0.198    🟢
// ΔBS vs Pinnacle        -0.012    🟢
// Edge Realization         0.87    🟢
// Max Drawdown             6.2%    🟢
// Market Alignment Value  +1.5¢    🟢
// Preliminary Accuracy     0.96    🟢
// No-dir Edge Real.        1.12    🟢
//
// Overall: ✅ HEALTHY
```

**Thresholds (from Phase 4 v2 doc):**

| Metric | Green | Yellow | Red |
|--------|-------|--------|-----|
| Brier Score | Phase 1.5 ± 0.02 | ± 0.05 | beyond |
| ΔBS vs Pinnacle | < 0 | 0–0.02 | > 0.02 |
| Edge Realization | 0.7–1.3 | 0.5–0.7 | < 0.5 |
| Max Drawdown | < 10% | 10–20% | > 20% |
| Alignment Value | ALIGNED > DIVERGENT + 1¢ | ≈ 0 | ALIGNED < DIVERGENT |
| Preliminary Accuracy | > 0.95 | 0.90–0.95 | < 0.90 |
| No-dir Edge Real. | 0.7–1.3 | > 1.5 | < 0.5 |

**Data source:** REST `/api/analytics/health` → queries `daily_analytics` table.

### D4.2: CalibrationPlot (3B)

```typescript
// src/components/Layer3_Analytics/CalibrationPlot.tsx

// Reliability diagram: predicted probability bins vs actual frequency
// X-axis: predicted P (0.0 to 1.0, 10 bins)
// Y-axis: actual frequency
// Perfect calibration = diagonal line
//
// Show 95% CI bands based on binomial distribution per bin
// Only show bins with ≥ 20 observations
//
// Tab selector: 1X2 / Over-Under / BTTS
```

**Data source:** REST `/api/analytics/calibration` → queries `trade_logs` + `positions`.

```python
# src/dashboard/api/analytics.py

@app.get("/api/analytics/calibration")
async def get_calibration(market: str = "all"):
    """
    Returns bins of (predicted_prob_range, actual_frequency, n_obs).
    Bins P_true into 10 buckets, computes actual outcome frequency.
    """
    trades = await db.query("""
        SELECT P_true, 
               CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END as outcome
        FROM trade_logs t
        JOIN positions p ON t.match_id = p.match_id AND t.market_ticker = p.market_ticker
        WHERE p.settlement IS NOT NULL
    """)
    # Bin and aggregate
    ...
```

### D4.3: CumulativePnL + Drawdown (3C)

```typescript
// src/components/Layer3_Analytics/CumulativePnL.tsx

// Main line: cumulative realized P&L (blue solid)
// Drawdown regions: red shaded area between P&L and running max
// Reference: Phase 1.5 simulation P&L (green dashed, if available)
// Phase transition markers: vertical dashed lines (A→B, B→C)
//
// X-axis: weeks or dates
// Y-axis: cumulative $
```

**Data source:** REST `/api/analytics/pnl_cumulative`

### D4.4: DirectionalAnalysis (3D)

```typescript
// src/components/Layer3_Analytics/DirectionalAnalysis.tsx

// Side-by-side panels:
//
// Buy Yes Direction          │  Buy No Direction
// Trades: 145                │  Trades: 87
// Win Rate: 58.6%            │  Win Rate: 54.0%
// Edge Realization: 0.92 🟢  │  Edge Realization: 1.15 🟢
// Avg EV at Entry: 3.1¢     │  Avg EV at Entry: 2.8¢
// Avg Actual Return: 2.9¢   │  Avg Actual Return: 3.2¢
//
// + EV distribution histogram for each direction
//
// Warning indicators:
//   No-dir Edge Real > 1.5 → "z is too conservative, consider lowering"
//   No-dir Edge Real < 0.5 → "z is too aggressive, consider raising"
```

### D4.5: Bet365AlignmentEffect (3E)

```typescript
// src/components/Layer3_Analytics/Bet365Effect.tsx

// Bar chart:
//   ALIGNED   avg return: +3.2¢/trade (n=98)
//   DIVERGENT avg return: +1.1¢/trade (n=47)
//
// Alignment Value: +2.1¢  🟢
//   → "Market alignment check is adding value."
//
// Win rate comparison:
//   ALIGNED: 61.2% | DIVERGENT: 48.9% | Δ: +12.3pp
```

### D4.6: PrelimAccuracy (3F)

```typescript
// src/components/Layer3_Analytics/PrelimAccuracy.tsx

// Stacked bar: Total Preliminary Events: 156
//   ████████████████████████████░░░░
//   Confirmed Match: 149 (95.5%)
//   VAR Cancelled: 4 (2.6%)
//   False Alarm: 3 (1.9%)
//
// Rapid Entry Readiness checklist:
//   ✅ Accuracy > 0.95 (0.955)
//   ✅ VAR rate < 0.03 (0.026)
//   ✅ Hypothetical P&L > 0 (+$42.30)
//   ✅ Trades >= 200 (287)
//   → "All conditions met. Rapid Entry: ACTIVATABLE"
//
// Rapid Entry Hypothetical P&L chart (cumulative over weeks)
```

### D4.7: ParamHistory (3G)

```typescript
// src/components/Layer3_Analytics/ParamHistory.tsx

// Two small line charts:
//   K_frac over time (0.25 → 0.42)
//   z over time (1.645 → 1.4)
//
// Current Parameters table:
//   K_frac:           0.42  (started: 0.25)
//   z:                1.4   (started: 1.645)
//   DIVERGENT mult:   0.5   (unchanged)
//   Cooldown:         13s   (started: 15s)
//   Rapid Entry:      OFF
//   bet365 Auto Exit: OFF
//
// Change log with reasons:
//   Week 12: K_frac 0.35 → 0.40
//     Reason: Edge Realization = 0.88 (≥ 0.8) for 3 weeks
```

**Data source:** REST `/api/analytics/params/history` → queries `daily_analytics` + config snapshots.

### D4.8: Backend Analytics Endpoints

```python
# src/dashboard/api/analytics.py

@app.get("/api/analytics/health")
async def get_health(): ...

@app.get("/api/analytics/calibration")
async def get_calibration(market: str = "all"): ...

@app.get("/api/analytics/pnl_cumulative")
async def get_cumulative_pnl(): ...

@app.get("/api/analytics/directional")
async def get_directional(): ...

@app.get("/api/analytics/alignment_effect")
async def get_alignment_effect(): ...

@app.get("/api/analytics/preliminary")
async def get_preliminary_stats(): ...

@app.get("/api/analytics/params/history")
async def get_param_history(): ...

@app.get("/api/analytics/params/current")
async def get_current_params(): ...
```

### D4.9: AnalyticsPage Assembly

```typescript
// src/pages/AnalyticsPage.tsx

// Tab layout within Analytics:
//   [Health] [Calibration] [P&L] [Directional] [Alignment] [Preliminary] [Params]
//
// Or: single scrollable page with all sections
```

### Verification

- HealthDashboard shows 7 gauges with correct colors
- CalibrationPlot renders dots near the diagonal
- Cumulative P&L chart shows correct settled P&L trajectory
- Directional analysis shows separate Yes/No statistics
- Alignment effect bar chart compares ALIGNED vs DIVERGENT
- Preliminary accuracy shows correct breakdown
- Parameter history shows timeline of changes

---

## Sprint D5: Polish + Advanced Features (3–4 days)

### D5.1: Match Panel Focus Mode

Click a match panel → expands to full width:
- PriceChart becomes large (takes full width)
- MuChart, SignalPanel, EventLog move to sidebar
- SourceStatus stays in header
- Click again or press Esc → return to grid

### D5.2: Match Panel Minimization

- HALFTIME matches auto-minimize to header-only
- FINISHED matches collapse with final score + P&L summary
- Manual minimize/maximize toggle

### D5.3: EventLog Filtering

- Toggle buttons to show/hide event types
- Default: TICK hidden (too noisy)
- Search box for text filtering
- Export to CSV button

### D5.4: Responsive Layout

- Desktop (>1200px): 2×2 grid for matches
- Tablet (768–1200px): single column, stacked panels
- Mobile (not a priority but don't break)

### D5.5: Mode Badge + Theme

```typescript
// PAPER mode: purple accent color throughout
//   Header badge: 🟣 PAPER TRADING
//   P&L labels: "Paper P&L"
//   Bankroll label: "Paper Bankroll"
//
// LIVE mode: default accent
//   Header badge: 🟢 LIVE
//   Actual bankroll from Kalshi
```

### D5.6: Dark Mode (Optional)

If time permits. Charts look better on dark backgrounds.

### Verification

- Focus mode works smoothly
- Halftime panels auto-minimize
- EventLog filters work
- Dashboard looks correct on 1080p and 1440p screens

---

## Sprint D6: Alerts Integration + Notification Panel (2–3 days)

### D6.1: In-App Notification Panel

```typescript
// Top-right bell icon → dropdown panel
//
// Recent alerts:
//   🔴 12:45  Drawdown 12.3% — review required
//   ⚠️  12:30  PRELIMINARY >30s for ARS-CHE
//   ℹ️  12:15  ENTRY: ARS-CHE Over2.5 YES @45¢ ×15
//   ✅  11:50  EXIT: LIV-MCI Over2.5 settled +$8.25
//
// Click → navigate to relevant match panel or analytics
// Badge count for unread alerts
```

**Data source:** Subscribe to Redis `alerts` channel + REST `/api/alerts/recent`.

### D6.2: Alert Configuration UI (Phase C)

```typescript
// Settings page → Alert Configuration
//
// Drawdown threshold:     [10%] (slider)
// PRELIMINARY timeout:    [30s] (input)
// Notify on entry:        [✓]
// Notify on exit:         [✓]
// Notify on profit only:  [ ]
// Slack enabled:          [✓]
// Telegram enabled:       [✓]
```

### D6.3: Daily Summary Email/Slack

The Alert Service (Step 5.3, already built) sends the daily summary.
The dashboard provides a visual version:

```typescript
// src/components/Layer3_Analytics/DailySummary.tsx
//
// Today's Summary:
//   Matches: 4  |  Trades: 7  |  P&L: +$23.50
//   Win Rate: 57%  |  Avg EV: 2.8¢  |  Drawdown: 2.1%
//
// Date picker to view historical daily summaries
```

---

## Sprint D7: Advanced Analytics + Replay (3–4 days)

### D7.1: Match Replay Mode

```typescript
// After a match ends, click "Replay" button on the match panel
// → Loads tick_snapshots from PostgreSQL
// → Renders PriceChart with full historical data
// → Playback controls: ▶ Play | ⏸ Pause | ⏩ 2x | ⏪ Rewind
// → Event markers visible on the timeline
// → Can scrub to any point in the match
```

**Data source:** REST `/api/matches/{id}/replay` → queries `tick_snapshots` hypertable.

```python
@app.get("/api/matches/{match_id}/replay")
async def get_match_replay(match_id: str):
    """Returns all tick snapshots for a completed match."""
    return await db.query("""
        SELECT time, t, score_h, score_a, state_x, delta_s,
               mu_h, mu_a, P_true, P_kalshi, P_bet365,
               sigma_MC, engine_phase, event_state
        FROM tick_snapshots
        WHERE match_id = $1
        ORDER BY time
    """, match_id)
```

### D7.2: Multi-Season Trend Analysis

```typescript
// src/components/Layer3_Analytics/SeasonTrend.tsx
//
// Monthly aggregation:
//   Month | Trades | P&L | Edge Real. | Brier | Drawdown
//   Jan   | 45     | +$120 | 0.91    | 0.195 | 3.2%
//   Feb   | 52     | +$85  | 0.82    | 0.201 | 5.1%
//   ...
//
// Line chart: monthly P&L + rolling 30-day Brier Score
```

### D7.3: Parameter Adjustment Approval UI (Phase C)

```typescript
// When adaptive_parameter_update() proposes a change:
// 
// ┌────────────────────────────────────────────────┐
// │  Parameter Adjustment Proposed                  │
// │                                                 │
// │  K_frac: 0.35 → 0.40                          │
// │  Reason: Edge Realization = 0.88 for 3 weeks   │
// │                                                 │
// │  [Approve]  [Reject]  [Defer 1 week]           │
// └────────────────────────────────────────────────┘
//
// In Phase B: all adjustments require manual approval
// In Phase C: auto-approve if within safe bounds
```

### Verification

- Match replay plays smoothly through a completed match
- Season trends show correct monthly aggregations
- Parameter approval UI shows pending changes

---

## Mapping to System Phases

| System Phase | Dashboard Sprint | What's Available |
|-------------|-----------------|-----------------|
| **Phase 0** (Paper) | D1 + D2 + D3 partial | PriceChart + Header + EventLog + Signals + Positions + P&L |
| **Phase A** (Conservative Live) | D3 complete + D4 partial | + RiskDashboard + HealthGauges + CumulativePnL |
| **Phase B** (Adaptive) | D4 complete + D5 + D6 | + All Analytics + Alerts panel + Polish |
| **Phase C** (Mature) | D7 | + Replay + Trends + Param approval UI |

### Minimum Viable for Each System Phase

**Phase 0 (Paper Trading) — must have before going live with paper:**

```
✅ D1: Backend + skeleton
✅ D2: PriceChart (1B) + MatchHeader (1A) + EventLog (1E)
✅ D3 partial: SignalPanel (1D) + PositionTable (2B) + PnLTimeline (2C)
```

**Phase A (First Real Dollar) — must have before live trading:**

```
✅ D3 complete: RiskDashboard (2A) + SourceStatus (1F) + MuChart (1C)
✅ D4 partial: HealthDashboard (3A) + CumulativePnL (3C)
✅ D6 partial: In-app notifications
```

**Phase B (Adaptive) — build during Phase A operation:**

```
✅ D4 complete: All Layer 3 analytics
✅ D5: Polish (focus mode, responsive, dark mode)
✅ D6 complete: Alert config UI
```

**Phase C (Mature) — build during Phase B operation:**

```
✅ D7: Replay + Season trends + Param approval
```

---

## Timeline Summary

| Sprint | Duration | Components |
|--------|----------|------------|
| D1 | 3–4 days | FastAPI + React scaffold + WebSocket |
| D2 | 4–5 days | PriceChart ⭐ + MatchHeader + EventLog |
| D3 | 4–5 days | MuChart + SignalPanel + SourceStatus + RiskDash + Positions + P&L |
| D4 | 4–5 days | All 7 Layer 3 analytics components + backend endpoints |
| D5 | 3–4 days | Focus mode + minimize + filters + responsive + mode badge |
| D6 | 2–3 days | In-app notifications + alert config + daily summary |
| D7 | 3–4 days | Match replay + season trends + param approval UI |

**Total: ~24–30 working days (5–6 weeks)**

But you don't need all of it before paper trading.
The critical path is D1 + D2 = **7–9 days** to get the PriceChart working,
which is the minimum viable dashboard for Phase 0.