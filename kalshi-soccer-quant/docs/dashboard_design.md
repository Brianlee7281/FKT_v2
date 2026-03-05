# Dashboard Design — Kalshi Soccer Quant Automated Trading System

## Overview

A dashboard for system operators to perform real-time situational judgment, risk monitoring, and post-trade analysis
by visualizing the data streams flowing through Phases 1-4.

Both paper trading (Phase 0) and live trading (Phase A-C) run in the same UI,
with only order submission behavior differing by mode.

### Design Principles

1. **Question-driven design:** every dashboard view answers a specific operator question.
2. **Progressive build-out:** the dashboard expands incrementally in step with system evolution phases.
3. **3-layer structure:** separate layers for real-time (in-match), portfolio (global), and analytics (post).
4. **Alert integration:** because operators cannot watch the dashboard at all times, key events must be push-alerted.

---

## Architecture

### 3-Layer Structure

```
+-------------------------------------------------------------+
|  Layer 1: Live Match View (in-match real-time)              |
|  • 1 panel per match — up to 10 concurrent matches          |
|  • updates every second                                      |
|  • Purpose: "What is happening right now?"                  |
+-------------------------------------------------------------+
|  Layer 2: Portfolio View (entire portfolio)                 |
|  • integrated view across all matches                       |
|  • real-time P&L, exposure, risk limits                     |
|  • Purpose: "Where and how much capital is tied up?"       |
+-------------------------------------------------------------+
|  Layer 3: Analytics View (post-analysis + system health)    |
|  • after match end + long-term trends                       |
|  • visualization of 11 metrics from Step 4.6                |
|  • Purpose: "Is the system healthy, and what should be fixed?" |
+-------------------------------------------------------------+
```

### Data Flow

```
+------------------------------------------------------------+
|  Trading Engine (Python asyncio)                           |
|                                                            |
|  Phase 3 + Phase 4 — single process                        |
|                                                            |
|  Every second:                                             |
|  +-- state_snapshot -> Redis Pub/Sub                       |
|  |   {t, S, X, ΔS, μ_H, μ_A, P_true, σ_MC,                |
|  |    engine_phase, event_state, cooldown, ob_freeze}     |
|  |                                                         |
|  +-- signal_snapshot -> Redis Pub/Sub                      |
|  |   {EV, direction, P_cons, bet365_confidence,           |
|  |    order_allowed}                                       |
|  |                                                         |
|  +-- orderbook_snapshot -> Redis Pub/Sub                   |
|      {P_kalshi_buy, P_kalshi_sell, P_bet365, depth}       |
|                                                            |
|  On event:                                                 |
|  +-- event_log -> Redis Pub/Sub + PostgreSQL              |
|      {type, source, confidence, timestamp, ...}           |
|                                                            |
|  On order:                                                 |
|  +-- trade_log -> PostgreSQL                              |
|      {all TradeLog fields}                                 |
|                                                            |
|  Live Odds WS (separate coroutine):                        |
|  +-- live_odds_snapshot -> Redis Pub/Sub                   |
|      {score, minute, period, bet365_odds, ball_pos}       |
+------------------+-----------------------------------------+
                   |
          +--------v--------+
          |     Redis       |  real-time data
          |     Pub/Sub     |  (<1s TTL)
          +--------+--------+
                   |
          +--------v--------+
          |  Dashboard      |  FastAPI + WebSocket
          |  Server         |  -> real-time push to browser
          +--------+--------+
                   |
          +--------v--------+
          |  React Client   |  Recharts / Lightweight Charts
          |  (Browser)      |
          +-----------------+

          +-----------------+
          |   PostgreSQL    |  persistent storage (for Layer 3)
          |   + TimescaleDB |
          +-----------------+
```

### Tech Stack

| Layer | Technology | Rationale |
|------|------|------|
| Real-time messaging | Redis Pub/Sub | sub-second latency, lightweight |
| Persistent storage | PostgreSQL + TimescaleDB | optimized for time series, analytics queries |
| Dashboard server | FastAPI + WebSocket | unified Python ecosystem, async-friendly |
| Frontend | React + Recharts | real-time charting, reusable components |
| Deployment | Single server (initial) | engine + dashboard on same machine |

---

## Paper Trading vs Live Trading Modes

Since both modes share the same data structure, virtual trades accumulated in paper mode
can be analyzed directly in Layer 3 Analytics.

```python
class TradingMode(Enum):
    PAPER = "paper"    # Phase 0: no order submission, virtual fills
    LIVE = "live"      # Phase A-C: real order submission

class ExecutionLayer:
    def __init__(self, mode: TradingMode):
        self.mode = mode

    async def execute_order(self, signal, amount, ob_sync):
        if self.mode == TradingMode.PAPER:
            # Virtual fill: assume immediate fill at current quote
            fill = PaperFill(
                price=ob_sync.kalshi_best_ask,
                quantity=int(amount / signal.P_kalshi),
                timestamp=time.time(),
                is_paper=True
            )
            record_position(signal, fill)
            return fill
        else:
            return await real_execute_order(signal, amount, ob_sync)
```

| Item | PAPER mode | LIVE mode |
|------|-----------|----------|
| Header color | Purple + "PAPER TRADING" badge | Standard color |
| Order submission | No | Yes |
| Fill logic | Immediate virtual fill at current quote | Real submission via Kalshi REST API |
| P&L label | "Virtual P&L" | Real P&L |
| Bankroll | Config value (e.g., $5,000) | Actual Kalshi balance |
| Data structure | Same TradeLog (is_paper=True) | Same TradeLog |
| Layer 3 analytics | Same behavior | Same behavior |

---

## Layer 1: Live Match View — Per-Match Real-Time Panel

This is the screen viewed most during live match operation.
One panel per match; tiled layout when up to 10 weekend matches run simultaneously.

### 1A: Match Status Header

```
+-------------------------------------------------------------+
|  [GREEN] Arsenal vs Chelsea            67:23    [BALL] 1-1  |
|  EPL  |  SECOND_HALF  |  11v11  |  IDLE                    |
|  cooldown: OFF  |  ob_freeze: OFF  |  pricing: ANALYTICAL  |
+-------------------------------------------------------------+
```

**Color coding by state:**

| State | Header Color | Meaning |
|------|----------|------|
| IDLE + active | [GREEN] border | normal operation, orders allowed |
| PRELIMINARY_DETECTED | [YELLOW] background | event detected, awaiting confirmation |
| COOLDOWN | [BLUE] border | in cooldown, orders blocked |
| OB_FREEZE | [RED] border | quote anomaly, orders blocked |
| HALFTIME | [GRAY] background | halftime freeze |
| FINISHED | [DARK] background | match finished, settlement complete |

**Data-source mapping:**

| Display Item | Source | Update Frequency |
|----------|------|----------|
| Team names | Phase 2 Step 2.1 | fixed |
| Minute:second | Live Odds WS `info.minute` | <1s |
| Score | Live Odds WS `info.score` | <1s |
| League | Phase 2 match metadata | fixed |
| engine_phase | Phase 3 Step 3.1 | on event |
| X(t) state (11v11, etc.) | Phase 3 Step 3.3 | on red card |
| event_state | Phase 3 Step 3.1 | on event |
| cooldown / ob_freeze | Phase 3 Step 3.1 | on event |
| pricing_mode | Phase 3 Step 3.4 | switches on event |

---

### 1B: Model vs Market Comparison Chart (Core Visualization)

**This is the most important chart in the dashboard.** It shows real-time movement of
the three prices: P_true, P_kalshi, and P_bet365.

```
P (probability)
0.70 |
     |              /-- P_true (model, blue)
0.60 |    ---------/
     |   /             -- P_kalshi mid (Kalshi, red)
0.50 |--/--------------
     |                  -- P_bet365 (bet365, green)
0.40 |---------------------------------------
     |
0.30 |
     +--+--+--+--+--+--+--+--+--+--+-- t (min)
        0 10 20 30 40 HT 55 65 75 85 90+
                      |
                  halftime segment
                  (gray shading)
```

**Chart elements:**

| Element | Visualization | Source |
|------|--------|------|
| P_true line | solid blue (thick) | Phase 3 Step 3.4 (every 1s) |
| P_kalshi mid line | solid red | (bid+ask)/2, Phase 4 Step 4.1 (every 1s) |
| P_kalshi bid-ask spread | red shading (thin band) | Phase 4 Step 4.1 |
| P_bet365 line | green dashed | Goalserve Live Odds WS (<1s) |
| Edge zone | light blue shading | where P_true > P_kalshi^ask |
| Goal event | vertical line + [BALL] | PRELIMINARY (dashed) -> CONFIRMED (solid) |
| Red card | vertical line + [RED_CARD] | Live Score CONFIRMED |
| Halftime | gray shaded segment | engine_phase == HALFTIME |
| Trade marker | [UP] (entry), [DOWN] (exit) | Phase 4 Step 4.5 |

**Market tabs:** switch between Over 2.5 / Home Win / Draw / Away Win.
Each tab displays P_true, P_kalshi, P_bet365 for that market.

**PRELIMINARY visualization:**

```
     |
     |         [BALL] (dashed, yellow)    [BALL] (solid, green)
     |         | PRELIMINARY              | CONFIRMED
0.55 |---------|                          |
     |         |    ob_freeze             | cooldown
     |         |<----- segment ---------->|<-- 15s -->|
     |         |  (yellow shading)        | (blue shading)
```

- PRELIMINARY segment: yellow background shading + dashed goal marker
- On CONFIRMED: dashed -> solid transition + green marker
- On VAR cancellation: dashed -> red X marker + remove yellow shading

---

### 1C: Intensity Function Monitor

A compact chart showing real-time decay of μ_H and μ_A:

```
μ (remaining expected goals)
1.5 |\
    | \-- μ_H (home, blue)
1.0 |  \         /-- goal event: delta jump
    |   --\      |
0.5 |      \-----|--\-- μ_A (away, red)
    |       ------   ------
0.0 |------------------------------
    +--+--+--+--+--+--+--+-- t
       0 15 30 45 60 75 90
```

| Event | Visual Effect |
|--------|-----------|
| time passes | smooth μ decrease (theta decay) |
| goal | δ change causes step jump in opposite directions for μ_H, μ_A |
| red card | gamma^H, gamma^A change causes step jump in opposite directions |
| basis boundary | b_i change causes slight discontinuity |

**Data source:** Phase 3 Step 3.2 μ_H, μ_A (every 1s)

**Why this chart matters:** it is the only real-time way to verify
whether the model processes events correctly. If μ does not jump after a goal, there is a bug.

---

### 1D: Trading Signal + Position Panel

```
+-------------------------------------------------------------+
|  Active Signals                                              |
|                                                              |
|  Over 2.5:  BUY YES | EV: 3.2c | [GREEN] HIGH | -> 2 contracts |
|  Home Win:  HOLD    | EV: 0.8c |    -         |                |
|  BTTS:      BUY NO  | EV: 2.1c | [YELLOW] LOW | -> 1 contract  |
|                                                              |
|  Open Positions                                              |
|  +-------------------------------------------------------+  |
|  | Over 2.5 YES | Entry: 45c | Now: 52c | P&L: +$1.05   |  |
|  | Qty: 15      | EV@entry: 3.2c         | bet365: 53c [OK] |
|  +-------------------------------------------------------+  |
|  | BTTS NO      | Entry: 38c | Now: 35c | P&L: +$0.45   |  |
|  | Qty: 8       | EV@entry: 2.1c         | bet365: 36c [OK] |
|  +-------------------------------------------------------+  |
+-------------------------------------------------------------+
```

| Item | Source | Update |
|------|------|------|
| Signal direction + EV | Phase 4 Step 4.2 | every 1s |
| bet365 confidence (HIGH [GREEN] / LOW [YELLOW]) | Phase 4 Step 4.2 | every 1s |
| Recommended contracts | Phase 4 Step 4.3 | every 1s |
| Position entry/current/P&L | Phase 4 Step 4.5 | every 1s |
| bet365 reference price + [OK]/[WARN] | Phase 4 Step 4.1 | <1s |

**bet365 [OK]/[WARN] indicator:**
- [OK] (green): bet365 agrees with position direction
- [WARN] (yellow): bet365 divergence warning triggered (Step 4.4 trigger 4)

---

### 1E: Event Log (Real-Time Stream)

```
+-------------------------------------------------------------+
|  Event Log                                    [Auto-scroll] |
|                                                              |
|  67:23  TICK    μ_H=0.42 μ_A=0.38 P(O2.5)=0.582             |
|  67:22  TICK    μ_H=0.43 μ_A=0.38 P(O2.5)=0.583             |
|  65:01  ORDER   BUY YES Over2.5 @45c x15 (EV=3.2c, HIGH)    |
|  65:00  SIGNAL  Over2.5 BUY_YES EV=3.2c bet365=HIGH         |
|  65:00  CONFIRMED  Goal (Away, Chelsea, Palmer)             |
|                    S=1-1, ΔS=0, cooldown=15s                |
|  64:55  PRELIMINARY  score 1-0->1-1 (Live Odds WS)          |
|                      ob_freeze=True, μ pre-computing...     |
|  64:54  OB_FREEZE  bet365 Δodds=15.2% -> freeze             |
|  ...                                                         |
+-------------------------------------------------------------+
```

**Event-type color coding:**

| Type | Color | Meaning |
|------|------|------|
| PRELIMINARY | [YELLOW] background | primary detection (awaiting confirmation) |
| CONFIRMED | [GREEN] background | confirmed (state update complete) |
| VAR_CANCELLED | [RED] text | VAR cancellation (state rollback) |
| OB_FREEZE | [RED] background | quote anomaly detected |
| COOLDOWN | [BLUE] background | cooldown enter/exit |
| SIGNAL | [PURPLE] text | trading signal generated |
| ORDER | [BOLD] text | order submitted/filled/cancelled |
| TICK | [LIGHT_GRAY] | regular tick (collapsible) |

**Filter:** toggle by event type to hide TICK or show only selected types.

---

### 1F: 3-Layer Detection Status Indicator (Compact)

```
+-----------------------------------------+
|  Data Sources                           |
|                                         |
|  Live Odds WS:  [GREEN] Connected  <1s  |
|  Kalshi WS:     [GREEN] Connected  ~1s  |
|  Live Score:    [GREEN] Polling    3s   |
|                                         |
|  Last Events:                           |
|  • Live Odds:  score change   2s ago    |
|  • Kalshi:     ob update      0.3s ago  |
|  • Live Score: poll ok        1.2s ago  |
+-----------------------------------------+
```

| Status | Indicator |
|------|------|
| healthy connection | [GREEN] |
| delayed (>5s no message) | [YELLOW] |
| failure (>10s or error) | [RED] |

---

## Layer 2: Portfolio View — Whole Portfolio

### 2A: Risk Dashboard

```
+-------------------------------------------------------------+
|  Portfolio Overview                                          |
|                                                              |
|  Bankroll: $5,000.00  |  Mode: [PURPLE] PAPER               |
|                                                              |
|  +-- Risk Limits ----------------------------------------+  |
|  |                                                       |  |
|  |  L1 single order (3%):   ####......  $87 / $150      |  |
|  |  L2 per match (5%):                                   |  |
|  |    ARS-CHE:             ######....  $156 / $250       |  |
|  |    LIV-MCI:             ####......  $98 / $250        |  |
|  |    BAR-RMA:             ##........  $45 / $250        |  |
|  |  L3 global (20%):       ######....  $412 / $1,000     |  |
|  |                                                       |  |
|  +-------------------------------------------------------+  |
|                                                              |
|  Summary:                                                    |
|  • Active Matches: 4                                         |
|  • Open Positions: 7                                         |
|  • Total Exposure: $412 (8.2%)                               |
|  • Unrealized P&L: +$23.50                                   |
|  • Today's Realized P&L: +$45.20                             |
+-------------------------------------------------------------+
```

**Progress bar colors:**
- 0-50%: green
- 50-80%: yellow
- 80%+: red

**Data sources:**

| Item | Source | Update |
|------|------|------|
| Bankroll | Kalshi REST balance (LIVE) / config value (PAPER) | on fill |
| Layer 1/2/3 usage | aggregate of positions from Phase 4 Step 4.5 | on fill |
| Active Matches | count of Phase 3 engine_phase | on match start/end |
| Total Exposure | aggregate all positions | every 1s (mark-to-market) |
| Unrealized P&L | Σ(current_price - entry_price) x qty | every 1s |
| Realized P&L | aggregate settled positions | on settlement |

---

### 2B: Per-Match Position Table

```
+----------+--------+------+-------+-------+--------+--------+--------+
| Match    | Market | Dir  | Entry | Curr  | P&L    | bet365 | Status |
+----------+--------+------+-------+-------+--------+--------+--------+
| ARS-CHE  | O2.5   | YES  | 45c   | 52c   | +$1.05 | 53c [OK] | 67'  |
| ARS-CHE  | HW     | NO   | 62c   | 58c   | +$0.60 | 56c [WARN] | 67' |
| LIV-MCI  | O2.5   | YES  | 55c   | 53c   | -$0.40 | 54c [OK] | 34'  |
| BAR-RMA  | HW     | YES  | 48c   | 51c   | +$0.45 | 50c [OK] | 12'  |
| -------- | ------ | ---- | ----- | ----- | ------ | ------ | ------ |
| JUV-NAP  | O2.5   | YES  | 52c   | 100c  | +$7.20 |  -     | FT [WIN] |
| JUV-NAP  | BTTS   | NO   | 45c   |  0c   | -$3.60 |  -     | FT [LOSS] |
+----------+--------+------+-------+-------+--------+--------+--------+
                                              Net: +$5.30
```

**Row color coding:**
- unrealized gain -> light green background
- unrealized loss -> light red background
- settled (win) -> green text + [WIN]
- settled (loss) -> red text + [LOSS]

**Sort/filter:**
- sortable by match / market / P&L
- filters for active (in-play) / settled

---

### 2C: Daily P&L Timeline

```
P&L ($)
+$50 |                              /--
     |                         /---/
+$30 |                    /---/      (unrealized, dashed)
     |               /---/
+$10 |          /---/
     |     /---/
  $0 |----/
     |   /
-$10 |--/
     | /
-$20 |/
     +--+--+--+--+--+--+--+--+--+--+
       12 13 14 15 16 17 18 19 20 21  Time (UTC)
```

| Line | Meaning | Style |
|------|------|--------|
| Realized P&L | cumulative settled profit | blue solid (step-like) |
| Unrealized P&L | value fluctuation of open positions | blue dashed (continuous) |
| Total P&L | realized + unrealized | thick blue solid |

---

## Layer 3: Analytics View — Post Analysis + System Health

Used after match end or during weekly/monthly reviews.
Visualizes the 11 metrics from Step 4.6.

### 3A: System Health Dashboard (Gauge Charts)

```
+-------------------------------------------------------------+
|  System Health Dashboard                    Updated: 5m ago |
|                                                              |
|  +-------------------------------------------------------+  |
|  |  Brier Score          [#######...]  0.198   [GREEN]   |  |
|  |  ΔBS vs Pinnacle      [######....]  -0.012  [GREEN]   |  |
|  |  Edge Realization     [#######...]  0.87    [GREEN]   |  |
|  |  Max Drawdown         [###.......]  6.2%    [GREEN]   |  |
|  |  bet365 Validation    [########..]  +2.1c   [GREEN]   |  |
|  |  Prelim Accuracy      [#########.]  0.96    [GREEN]   |  |
|  |  No-dir Edge Real.    [######....]  1.12    [GREEN]   |  |
|  +-------------------------------------------------------+  |
|                                                              |
|  Overall Status: [HEALTHY]                                   |
|  Last Recalibration: 2025-08-15 (42 days ago)               |
|  Cumulative Trades: 287                                      |
|  System Phase: B (Adaptive Live)                             |
+-------------------------------------------------------------+
```

**Thresholds for 7 metrics:**

| Metric | Healthy [GREEN] | Warning [YELLOW] | Risk [RED] |
|------|---------|---------|---------|
| Brier Score | Phase 1.5 ± 0.02 | ± 0.05 | outside |
| ΔBS vs Pinnacle | < 0 | 0~0.02 | > 0.02 |
| Edge realization | 0.7~1.3 | 0.5~0.7 | < 0.5 |
| Max Drawdown | < 10% | 10~20% | > 20% |
| bet365 validation value | HIGH > LOW + 2c | HIGH ≈ LOW | HIGH < LOW |
| Preliminary accuracy | > 0.95 | 0.90~0.95 | < 0.90 |
| No-direction realization | 0.7~1.3 | > 1.5 (too conservative) | < 0.5 (risky) |

---

### 3B: Calibration Plot (Reliability Diagram)

Continuously update the calibration plot defined in Phase 1 Step 1.5 with live data:

```
Observed frequency
1.0 |                                    /
    |                                 / /
0.8 |                              . / /  (ideal: diagonal)
    |                           .  / /
0.6 |                        .   / /
    |                     .    / /
0.4 |                  .     / /
    |               .      / /
0.2 |            .       / /
    |         .        / /
0.0 |------.---------/--/----------------
    +--+--+--+--+--+--+--+--+--+--+
      0.0   0.2   0.4   0.6   0.8   1.0
                    Predicted probability
```

- show a point when each bin accumulates at least 20 observations
- closer to diagonal (gray dashed) means better calibration
- confidence band (light shading): binomial 95% CI by bin size

**Market tabs:** separate calibration plots for 1X2 / Over-Under / BTTS.

---

### 3C: Cumulative P&L + Drawdown Chart

```
P&L ($)
+$600 |                                    /------
      |                              /----/
+$400 |                        /----/      |
      |                  /----/            | <- Max DD: 6.2%
+$200 |            /----/                 \|/--
      |      /----/                            /
   $0 |-----/
      |    /
-$100 |---/
      +--+--+--+--+--+--+--+--+--+--+
        W1 W3 W5 W7 W9 W11 W13 W15
```

| Line | Meaning | Style |
|------|------|--------|
| Realized cumulative P&L | blue solid | — |
| Drawdown interval | red shading (between P&L and prior peak) | — |
| Phase 1.5 simulation | green dashed (validation-period P&L) | baseline comparison |
| Phase transition marker | vertical dashed (A->B, B->C) | — |

---

### 3D: Directional Analysis (P_cons Diagnostics)

```
+------------------------------------+------------------------------------+
|  Buy Yes Direction                 |  Buy No Direction                  |
|                                    |                                    |
|  Trades: 145                       |  Trades: 87                        |
|  Win Rate: 58.6%                   |  Win Rate: 54.0%                   |
|  Edge Realization: 0.92 [GREEN]    |  Edge Realization: 1.15 [GREEN]    |
|                                    |                                    |
|  Avg EV at Entry: 3.1c             |  Avg EV at Entry: 2.8c             |
|  Avg Actual Return: 2.9c           |  Avg Actual Return: 3.2c           |
|                                    |                                    |
|  +-- EV Distribution ------------+ |  +-- EV Distribution ------------+ |
|  |  ##                          | |  |  #                           | |
|  |  ####                        | |  |  ###                         | |
|  |  ######                      | |  |  #####                       | |
|  |  ########                    | |  |  #######                     | |
|  |  -------------               | |  |  -------------               | |
|  |  0 2 4 6 8 10 (c)            | |  |  0 2 4 6 8 10 (c)            | |
|  +------------------------------+ |  +------------------------------+ |
+------------------------------------+------------------------------------+
```

**Warning conditions:**
- No-direction edge realization > 1.5 -> warn "z should be lowered"
- No-direction edge realization < 0.5 -> warn "z should be raised"
- directional gap >= 0.3 -> suggest "separate z by direction"

---

### 3E: bet365 Cross-Validation Effect

```
+-------------------------------------------------------------+
|  bet365 Cross-Validation Effect                              |
|                                                              |
|  Average return (c/trade)                                    |
|  +4c |  ##                                                   |
|      |  ##                                                   |
|  +3c |  ##                                                   |
|      |  ##     ##                                            |
|  +2c |  ##     ##                                            |
|      |  ##     ##                                            |
|  +1c |  ##     ##                                            |
|      |  ##     ##                                            |
|   0c |--##-----##----                                        |
|      |  HIGH   LOW                                           |
|      |  (n=98) (n=47)                                        |
|                                                              |
|  Validation Value: +2.1c/trade  [GREEN]                     |
|  -> bet365 validation adds value. Keep kelly_multiplier.    |
|                                                              |
|  Win-rate comparison:                                        |
|  HIGH: 61.2%  |  LOW: 48.9%  |  Gap: +12.3%p                |
+-------------------------------------------------------------+
```

---

### 3F: Preliminary -> Confirmed Tracking

```
+-------------------------------------------------------------+
|  Preliminary Detection Performance                           |
|                                                              |
|  Total Preliminary Events: 156                               |
|                                                              |
|  +-----------------------------------------------------+    |
|  |  ########################################........    |    |
|  |  Confirmed Match: 149 (95.5%)                       |    |
|  |  .... VAR Cancelled: 4 (2.6%)                       |    |
|  |  .... False Alarm: 3 (1.9%)                         |    |
|  +-----------------------------------------------------+    |
|                                                              |
|  Rapid Entry Readiness:                                      |
|  [OK] Accuracy > 0.95 (0.955)                               |
|  [OK] VAR rate < 0.03 (0.026)                               |
|  [OK] Hypothetical P&L > 0 (+$42.30)                        |
|  [OK] Trades >= 200 (287)                                   |
|  -> All conditions met. Rapid Entry: ACTIVATABLE            |
|                                                              |
|  Rapid Entry Hypothetical P&L:                              |
|  +$50 |          /------                                     |
|       |     /---/                                            |
|  +$25 |----/                                                 |
|       |   /                                                  |
|    $0 |--/                                                   |
|       +--+--+--+--+--+                                      |
|         W1 W4 W8 W12 W16                                    |
+-------------------------------------------------------------+
```

---

### 3G: Adaptive Parameter History

```
+-------------------------------------------------------------+
|  Parameter Evolution                                         |
|                                                              |
|  K_frac                              z (conservativeness)    |
|  0.50 |              /--    1.8 |--\                        |
|       |         /---/          |   \                        |
|  0.40 |    /---/              1.6 |    \--------            |
|       |---/                        |             \           |
|  0.30 |/                     1.4 |              \--         |
|  0.25 |                      1.2 |                          |
|       +--+--+--+--+            +--+--+--+--+               |
|         W1 W5 W10 W15            W1 W5 W10 W15              |
|                                                              |
|  Current Parameters:                                         |
|  +------------------------------------------------------+    |
|  |  K_frac:           0.42  (started: 0.25)            |    |
|  |  z:                1.4   (started: 1.645)           |    |
|  |  LOW multiplier:   0.5   (unchanged)                |    |
|  |  Cooldown:         13s   (started: 15s)             |    |
|  |  Rapid Entry:      OFF   (conditions met)           |    |
|  |  bet365 Auto Exit: OFF   (n=18, need 30)            |    |
|  +------------------------------------------------------+    |
|                                                              |
|  Phase: B (Adaptive Live)                                    |
|  -> Next milestone: 300 trades for Phase C                   |
|     Current: 287 / 300                                       |
+-------------------------------------------------------------+
```

**Per-parameter change history:** annotate each change with the reason:

```
Week 12: K_frac 0.35 -> 0.40
  Reason: Edge Realization = 0.88 (>= 0.8) for 3 consecutive weeks

Week 10: z 1.645 -> 1.4
  Reason: No-direction Edge Realization = 1.52 (> 1.5) — too conservative

Week 8: Cooldown 15s -> 13s
  Reason: Suppressed profitable rate = 62% (> 60%)
```

---

## Alerting System

Since operators cannot always watch the dashboard, key events must trigger push alerts.

### Alert Categories

| Event | Severity | Channel | Example Message |
|--------|--------|------|-----------|
| Position entry | Info | Slack | `ENTRY: ARS-CHE Over2.5 YES @45c x15 (EV=3.2c, HIGH)` |
| Position exit (profit) | Success | Slack | `EXIT: ARS-CHE Over2.5 YES settled @100c. P&L: +$8.25` |
| Position exit (loss) | Warning | Slack | `EXIT: LIV-MCI Over2.5 YES settled @0c. P&L: -$5.50` |
| Drawdown > 10% | Critical | Slack + Telegram | `Drawdown 12.3% ($615/$5000). Review required.` |
| Live Odds WS failure | Critical | Slack + Telegram | `Live Odds WS disconnected. Fallback to 2-layer mode.` |
| Live Score fails 5x | Critical | Slack + Telegram | `Live Score polling failed 5x. Match ARS-CHE frozen.` |
| PRELIMINARY > 30s | Warning | Slack | `PRELIMINARY state >30s for ARS-CHE. Possible VAR review.` |
| Health metric in risk zone | Critical | Slack + Telegram | `Edge Realization dropped to 0.45. Consider pausing.` |
| Daily P&L summary | Info | Slack (daily) | `Daily: 4 matches, 7 trades, P&L: +$23.50, DD: 2.1%` |
| Weekly report | Info | Slack (weekly) | `Weekly: 12 matches, 23 trades, P&L: +$142, Brier: 0.198` |

### Alert Implementation

```python
from enum import Enum

class AlertSeverity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

class AlertChannel(Enum):
    SLACK = "slack"
    TELEGRAM = "telegram"

ALERT_ROUTING = {
    AlertSeverity.INFO: [AlertChannel.SLACK],
    AlertSeverity.WARNING: [AlertChannel.SLACK],
    AlertSeverity.CRITICAL: [AlertChannel.SLACK, AlertChannel.TELEGRAM],
}

async def send_alert(severity: AlertSeverity, title: str, body: str,
                     match_id: str = None):
    channels = ALERT_ROUTING[severity]

    message = format_alert(severity, title, body, match_id)

    for channel in channels:
        if channel == AlertChannel.SLACK:
            await slack_webhook.post(message)
        elif channel == AlertChannel.TELEGRAM:
            await telegram_bot.send(message)

# Example usage
await send_alert(
    AlertSeverity.CRITICAL,
    "Max Drawdown Exceeded",
    f"Drawdown {dd_pct:.1f}% (${dd_amount:.0f}/${bankroll:.0f})\n"
    f"Action: All new entries frozen. Manual review required.",
    match_id="ARS-CHE-20251018"
)
```

---

## Implementation Roadmap — Progressive Expansion by System Phase

### Phase 0 (Paper Trading) — Minimal Dashboard

What must be built first:

| Priority | View | Why |
|---------|-----|------|
| Critical | **1B: P_true vs P_kalshi vs P_bet365 chart** | core judgment of whether model works |
| Critical | 1A: match status header | baseline situational awareness |
| Critical | 1E: event log | event-processing debugging |
| Critical | 1D: signals + virtual positions | virtual-trade tracking |
| Needed | 2B: position table (virtual) | full position overview |
| Needed | 2C: virtual P&L timeline | performance tracking |
| Optional | 1C: μ decay chart | internal model behavior check |

**Technical implementation:**
- React + Recharts (frontend)
- FastAPI + WebSocket (server)
- Redis Pub/Sub (real-time stream)
- SQLite (lightweight initial DB instead of PostgreSQL)

### Phase A (Conservative Live) — Add Risk Monitoring

```
Add:
+-- 2A: Risk dashboard (L1/L2/L3 visualization) <- essential with real money
+-- 3A: System health dashboard (7 gauges)
+-- 3C: Cumulative P&L + Drawdown
+-- Alerts: Slack integration (drawdown/order/failure alerts)
```

**Technical upgrades:**
- SQLite -> PostgreSQL + TimescaleDB
- Slack webhook integration

### Phase B (Adaptive Live) — Add Analysis Tools

```
Add:
+-- 3B: Calibration plot
+-- 3D: Directional analysis (Yes vs No)
+-- 3E: bet365 validation effect
+-- 3F: Preliminary accuracy + Rapid Entry hypothetical P&L
+-- 3G: Parameter history
+-- Alerts: add Telegram (critical events)
```

### Phase C (Mature Live) — Automation + Advanced Features

```
Add:
+-- auto-parameter adjustment history + approval UI
|   (auto suggestion -> operator approval -> apply)
+-- Rapid Entry ON/OFF toggle (directly in UI)
+-- multi-season trend analytics
+-- match replay mode (rewind historical timeline)
+-- auto-generated weekly/monthly PDF reports
```

---

## Layout Structure

### Main Screen Composition

```
+-----------------------------------------------------------------+
|  Navigation Bar                                                  |
|  [Live Matches] [Portfolio] [Analytics] [Settings]  [PURPLE] PAPER |
+-----------------------------------------------------------------+
|                                                                 |
|  +-- Live Matches Tab ---------------------------------------+  |
|  |                                                          |  |
|  |  +-- Match 1 -----------+  +-- Match 2 -----------+     |  |
|  |  | 1A: Header           |  | 1A: Header           |     |  |
|  |  | 1B: Price Chart      |  | 1B: Price Chart      |     |  |
|  |  | 1C: μ Chart          |  | 1C: μ Chart          |     |  |
|  |  | 1D: Signals/Pos      |  | 1D: Signals/Pos      |     |  |
|  |  +----------------------+  +----------------------+     |  |
|  |                                                          |  |
|  |  +-- Match 3 -----------+  +-- Match 4 -----------+     |  |
|  |  | ...                  |  | ...                  |     |  |
|  |  +----------------------+  +----------------------+     |  |
|  |                                                          |  |
|  |  +-- Event Log (shared, all matches) ------------------+ |  |
|  |  | 1E: integrated event log (match filter available)   | |  |
|  |  +------------------------------------------------------+ |  |
|  +------------------------------------------------------------+  |
|                                                                 |
|  +-- Portfolio Tab ------------------------------------------+  |
|  |  2A: Risk Dashboard  |  2B: Position Table               |  |
|  |  2C: P&L Timeline                                        |  |
|  +------------------------------------------------------------+  |
|                                                                 |
|  +-- Analytics Tab ------------------------------------------+  |
|  |  3A: Health Dashboard | 3B: Calibration | 3C: P&L        |  |
|  |  3D: Directional      | 3E: bet365 Effect | 3F: Prelim   |  |
|  |  3G: Parameter History                                    |  |
|  +------------------------------------------------------------+  |
|                                                                 |
+-----------------------------------------------------------------+
```

### Match Panel Expand/Collapse

- **Tile mode:** show concurrently in 2x2 or 3x3 grid (default)
- **Focus mode:** click a match to expand full-screen
  - 1B chart enlarged; 1C + 1D + 1E move to sidebar
- **Minimize mode:** auto-minimize during HALFTIME (header only)

---

## Summary: Questions the Dashboard Must Answer

| Question | Layer | Key View |
|------|-------|---------|
| "What is happening in this match right now?" | Layer 1 | 1A header + 1E log |
| "Is the model better than the market?" | Layer 1 | 1B three-price chart (core) |
| "Were events processed correctly?" | Layer 1 | 1C μ chart + 1E log |
| "What are we currently betting?" | Layer 1 | 1D signals/positions |
| "Where and how much money is exposed?" | Layer 2 | 2A risk + 2B positions |
| "How much did we make today?" | Layer 2 | 2C P&L timeline |
| "Is the system healthy?" | Layer 3 | 3A health dashboard |
| "Is the model biased?" | Layer 3 | 3B calibration + 3D directional |
| "Does bet365 validation add value?" | Layer 3 | 3E bet365 effect |
| "Can we turn on Rapid Entry?" | Layer 3 | 3F preliminary accuracy |
| "How should parameters be adjusted?" | Layer 3 | 3G parameter history |
