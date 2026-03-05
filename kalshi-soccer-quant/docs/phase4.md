# Phase 4: Arbitrage & Execution — Goalserve Full Package (v2)

> **v1 → v2 Change Log:**
>
> | # | Severity | Change |
> |---|----------|--------|
> | 1 | 🔴 Critical | Edge Reversal Buy No threshold: `(1-P_kalshi_bid)` → `P_kalshi_bid` |
> | 2 | 🔴 Critical | Expiry Eval: Added Buy No branch for `E_hold` formula |
> | 3 | 🟠 Important | bet365 Divergence Buy No threshold: `(1-entry)` → `entry` |
> | 4 | 🟠 Important | bet365 downgraded from “independent validation” to “market alignment check”, multiplier 1.0→0.8 |
> | 5 | 🟠 Important | Connected VWAP effective price to EV/Kelly (2-pass calculation) |
> | 6 | 🟠 Important | Paper fills: VWAP + slippage + partial fill simulation |
> | 7 | 🟡 Moderate | Rapid Entry: VAR safety wait + P_cons adjustment + stricter activation conditions |
> | 8 | 🔴 Critical | Auto-settlement P&L: directional formula branch for Buy No |

## Overview

After three prior phases that refine the mathematical probability model (`P_true`),
this is the execution front line where model output is converted into real money.

No matter how good the model is, poor design in order-book friction handling
or money management can still lead to ruin.

Using the probability from the Phase 3 engine,
we detect edge in Kalshi markets,
submit orders based on Kelly,
and feed post-match outcomes back into the system.
This is decomposed into 6 steps.

### Paradigm Shift: Defensive → Adaptive

The 3-layer detection system in the Goalserve full package fundamentally changes Phase 4 strategy:

```
Original: event detection (3–8s) > Kalshi MM (1–2s) → always defensive
Full package: event detection (<1s) ≤ Kalshi MM (1–2s) → can be aggressive depending on conditions
```

Aggressiveness is still increased **gradually** along the Phase 0 → A → B → C roadmap.

### Buy Yes vs Buy No — Probability Space Convention

In this document, all probabilities and prices are represented in the **Yes-probability space**:

| Symbol | Meaning | Space |
|------|------|------|
| P_true | Model-estimated true event probability | Yes probability |
| P_kalshi_buy | Kalshi Best Ask (Yes buy price) | Yes probability |
| P_kalshi_sell | Kalshi Best Bid (Yes sell price) | Yes probability |
| P_bet365 | bet365 implied probability | Yes probability |
| entry_price | Fill price at entry | Yes probability |
| settlement | Settlement at expiry (100¢ or 0¢) | Yes probability |

**Buy No position:**
- Equivalent to selling Yes → `entry_price` is the Yes sell price
- No wins (= Yes settles at 0¢) → profit = `entry_price`
- No loses (= Yes settles at 100¢) → loss = `(1 - entry_price)`

> **Core v2 principle:** In all Buy No formulas, do **not** apply `(1 - P)` conversion.
> `P_cons`, `P_kalshi`, `P_bet365`, and `entry_price` are all compared directly in Yes space.
> Conversion is only used in direction-specific payoff calculations (win/loss branches).

---

## Input Data

**Phase 3 outputs (every second):**

| Item | Description | Update Frequency |
|------|------|-------------|
| P_true(t) | True probability per market (Yes space) | every 1s |
| σ_MC(t) | Monte Carlo standard error (analytical mode: 0) | every 1s |
| order_allowed | NOT cooldown AND NOT ob_freeze AND event_state == IDLE | every 1s + on events |
| event_state | IDLE / PRELIMINARY / CONFIRMED | on events |
| pricing_mode | Analytical / Monte Carlo | switches on events |
| engine_phase | FIRST_HALF / HALFTIME / SECOND_HALF / FINISHED | on period change |
| μ_H, μ_A | Expected remaining goals | every 1s (logging) |
| **P_bet365(t)** | **bet365 in-play implied probability (Yes space)** | **every push (<1s)** |

**Kalshi API:**

| Endpoint | Use |
|-----------|------|
| WebSocket | Real-time order book (Bid/Ask + Depth) |
| REST `/portfolio/orders` | Submit/cancel orders |
| REST `/portfolio/positions` | Existing position lookup |
| REST `/portfolio/balance` | Account balance |

---

## Step 4.1: Live Order Book Synchronization

### Goal

Receive Kalshi order-book data in real time,
align it on the same timestamp axis as Phase 3 `P_true`,
and add Goalserve Live Odds bet365 prices as a market reference.

### Kalshi Quote Intake

**Bid/Ask separation:**

$$P_{kalshi}^{buy} = \frac{\text{Best Ask (¢)}}{100}, \quad P_{kalshi}^{sell} = \frac{\text{Best Bid (¢)}}{100}$$

**Order-book depth (Depth) — VWAP effective price:**

> **[v2 fix #5] Connect VWAP directly to EV calculation in Step 4.2.**

For order size $Q$, compute the weighted average effective price:

$$P_{effective}(Q) = \frac{\sum_{level} p_{level} \times q_{level}}{\sum_{level} q_{level}} \quad \text{(accumulated until Q contracts)}$$

```python
class OrderBookSync:
    def __init__(self):
        # Kalshi quotes
        self.kalshi_best_bid = None
        self.kalshi_best_ask = None
        self.kalshi_depth_ask = []  # [(price, qty), ...] ascending
        self.kalshi_depth_bid = []  # [(price, qty), ...] descending

        # bet365 reference prices
        self.bet365_implied = {}

    def compute_vwap_buy(self, target_qty: int) -> Optional[float]:
        """
        Effective buy price (VWAP) for target_qty contracts.
        Consumes ask levels from low to high.
        Returns None if depth is insufficient.
        """
        if not self.kalshi_depth_ask:
            return None

        filled = 0
        cost = 0.0
        for price, qty in self.kalshi_depth_ask:
            take = min(qty, target_qty - filled)
            cost += price * take
            filled += take
            if filled >= target_qty:
                break

        if filled < target_qty:
            return None  # insufficient depth

        return cost / filled

    def compute_vwap_sell(self, target_qty: int) -> Optional[float]:
        """Effective sell price for target_qty contracts (Bid VWAP)."""
        if not self.kalshi_depth_bid:
            return None

        filled = 0
        revenue = 0.0
        for price, qty in self.kalshi_depth_bid:
            take = min(qty, target_qty - filled)
            revenue += price * take
            filled += take
            if filled >= target_qty:
                break

        if filled < target_qty:
            return None
        return revenue / filled

    def update_bet365(self, live_odds_markets: dict):
        """Goalserve Live Odds WebSocket → convert to bet365 implied probabilities"""
        ft = live_odds_markets.get("1777", {})
        participants = ft.get("participants", {})

        home_odds, draw_odds, away_odds = None, None, None
        for pid, p in participants.items():
            name = p.get("short_name", "") or p.get("name", "")
            odds = float(p["value_eu"])
            if "Home" in name:
                home_odds = odds
            elif name in ("X", "Draw"):
                draw_odds = odds
            elif "Away" in name:
                away_odds = odds

        if home_odds and draw_odds and away_odds:
            raw_sum = 1/home_odds + 1/draw_odds + 1/away_odds
            self.bet365_implied["home_win"] = (1/home_odds) / raw_sum
            self.bet365_implied["draw"] = (1/draw_odds) / raw_sum
            self.bet365_implied["away_win"] = (1/away_odds) / raw_sum
```

### Liquidity Filter

Do not enter if total depth is below minimum threshold:

$$\text{Total Ask Depth} \geq Q_{min} \quad (\text{e.g., } Q_{min} = 20\text{ contracts})$$

### bet365 Reference Price — “Market Alignment Check” (Not Independent Validation)

> **[v2 fix #4] Reclassify bet365 from “independent validator” to “market alignment check.”**
>
> Reason: `P_true` and `P_bet365` are **both derived from the same Goalserve feed**,
> especially right after events, where both may simply reflect the same information.
> So this is not truly independent validation, but a directional alignment check.
> Therefore, set `kelly_multiplier` to 0.8 instead of 1.0.
>
> **When bet365 is most useful:**
> - No-event normal ticks: model uses MMPP time decay, bet365 uses market-making dynamics
>   → different information handling → meaningful alignment value
> - Immediately after events: both derived from same Goalserve event signal
>   → low independence → weaker alignment value

**Three uses of bet365 reference price:**

| Use | Method | Step |
|------|------|------|
| **Market alignment check** | Compare model direction vs bet365 direction | Step 4.2 |
| **Sizing adjustment** | 0.8x when aligned, 0.5x when divergent | Step 4.3 |
| **Exit support signal** | Warning when bet365 moves against position | Step 4.4 |

### Outputs

| Item | Description |
|------|------|
| $P_{kalshi}^{buy}(t)$ | Best ask for buying Yes (top of book) |
| $P_{kalshi}^{sell}(t)$ | Best bid for selling Yes (top of book) |
| $P_{effective}^{buy}(Q)$ | VWAP effective buy price for Q contracts |
| $P_{effective}^{sell}(Q)$ | VWAP effective sell price for Q contracts |
| $P_{bet365}(t)$ | bet365 in-play implied probability (by market) |
| liquidity_ok | Liquidity filter pass/fail |
| depth_profile | Size by order-book level |

---

## Step 4.2: Fee-Adjusted Edge Detection (EV Computation)

### Goal

Compare model `P_true` and market `P_kalshi`,
verify positive expected value after fees/slippage,
and classify edge reliability with bet365 market alignment.

### P_true^cons — Directional Conservative Adjustment

```python
def compute_conservative_P(P_true: float, sigma_MC: float,
                            direction: str, z: float = 1.645) -> float:
    """
    Buy Yes: higher P is favorable → use lower bound (conservatively reduce)
    Buy No:  lower P is favorable → use upper bound (conservatively increase)
    """
    if direction == "BUY_YES":
        return P_true - z * sigma_MC
    elif direction == "BUY_NO":
        return P_true + z * sigma_MC
    else:
        return P_true
```

> **Why direction must differ:**
> If one lower bound (`P_true - z·σ`) is used for both directions,
> Buy No gets artificially inflated `(1 - P_cons)`,
> systematically **overestimating** No-side EV.
> The larger MC uncertainty is, the more aggressively the system overbets No.

### Fee-Adjusted EV — 2-Pass VWAP Connection

> **[v2 fix #5] Use VWAP effective price (not best ask/bid) in EV calculation.**
>
> Circular dependency: EV → Kelly → qty → VWAP → EV
> Solution: 2-pass computation.

```python
def compute_signal_with_vwap(
    P_true: float, sigma_MC: float,
    ob_sync: OrderBookSync,
    c: float, z: float, K_frac: float,
    bankroll: float, market_ticker: str
) -> Signal:
    """
    Connect VWAP to EV with 2-pass computation.

    Pass 1: estimate rough quantity with best ask/bid
    Pass 2: compute final EV with VWAP for rough quantity
    """
    # ═══ Pass 1: rough evaluation with best ask/bid ═══
    P_best_ask = ob_sync.kalshi_best_ask
    P_best_bid = ob_sync.kalshi_best_bid

    # Buy Yes side
    P_cons_yes = P_true - z * sigma_MC
    rough_EV_yes = (
        P_cons_yes * (1 - c) * (1 - P_best_ask)
        - (1 - P_cons_yes) * P_best_ask
    )

    # Buy No side
    P_cons_no = P_true + z * sigma_MC
    rough_EV_no = (
        (1 - P_cons_no) * (1 - c) * P_best_bid
        - P_cons_no * (1 - P_best_bid)
    )

    # Direction selection (higher EV)
    if rough_EV_yes > rough_EV_no and rough_EV_yes > THETA_ENTRY:
        direction = "BUY_YES"
        rough_P_kalshi = P_best_ask
        P_cons = P_cons_yes
    elif rough_EV_no > THETA_ENTRY:
        direction = "BUY_NO"
        rough_P_kalshi = P_best_bid
        P_cons = P_cons_no
    else:
        return Signal(direction="HOLD")

    # Rough quantity
    rough_f = rough_kelly(direction, P_cons, rough_P_kalshi, c, K_frac, rough_EV_yes if direction == "BUY_YES" else rough_EV_no)
    rough_qty = int(rough_f * bankroll / rough_P_kalshi)
    if rough_qty < 1:
        return Signal(direction="HOLD")

    # ═══ Pass 2: final EV with VWAP ═══
    if direction == "BUY_YES":
        P_effective = ob_sync.compute_vwap_buy(rough_qty)
    else:
        P_effective = ob_sync.compute_vwap_sell(rough_qty)

    if P_effective is None:
        return Signal(direction="HOLD")  # insufficient depth

    # Final EV (VWAP-based)
    if direction == "BUY_YES":
        final_EV = (
            P_cons * (1 - c) * (1 - P_effective)
            - (1 - P_cons) * P_effective
        )
    else:  # BUY_NO
        final_EV = (
            (1 - P_cons) * (1 - c) * P_effective
            - P_cons * (1 - P_effective)
        )

    if final_EV <= THETA_ENTRY:
        return Signal(direction="HOLD")  # edge disappears after VWAP

    return Signal(
        direction=direction,
        EV=final_EV,
        P_cons=P_cons,
        P_kalshi=P_effective,  # ← VWAP effective price
        rough_qty=rough_qty,
        market_ticker=market_ticker
    )
```

### Market Alignment Check — bet365 Reference

> **[v2 fix #4] Renamed from “independent validation” to “market alignment check.”**
> `kelly_multiplier`: ALIGNED=0.8 (not 1.0), DIVERGENT=0.5.

```python
@dataclass
class MarketAlignment:
    status: str             # "ALIGNED", "DIVERGENT", "UNAVAILABLE"
    kelly_multiplier: float # ALIGNED→0.8, DIVERGENT→0.5, UNAVAILABLE→0.6

def check_market_alignment(
    P_true_cons: float,
    P_kalshi: float,
    P_bet365: Optional[float],
    direction: str
) -> MarketAlignment:
    """
    Check directional alignment between model and bet365.

    This is NOT independent validation:
    - both are derived from the same Goalserve feed
    - captures interpretation gap between model (MMPP) and market (trader+algo)
    - even when aligned, use 0.8 instead of 1.0 (prevent overconfidence)
    """
    if P_bet365 is None:
        return MarketAlignment(
            status="UNAVAILABLE",
            kelly_multiplier=0.6  # conservative when data is missing
        )

    # All comparisons are in Yes probability space
    if direction == "BUY_YES":
        model_says_high = P_true_cons > P_kalshi
        bet365_says_high = P_bet365 > P_kalshi
        aligned = model_says_high and bet365_says_high

    elif direction == "BUY_NO":
        model_says_low = P_true_cons < P_kalshi
        bet365_says_low = P_bet365 < P_kalshi
        aligned = model_says_low and bet365_says_low

    else:
        return MarketAlignment(status="UNAVAILABLE", kelly_multiplier=0.6)

    if aligned:
        return MarketAlignment(
            status="ALIGNED",
            kelly_multiplier=0.8  # [v2] not 1.0 (reflect limited independence)
        )
    else:
        return MarketAlignment(
            status="DIVERGENT",
            kelly_multiplier=0.5
        )
```

### Filtering Conditions

| Condition | Description |
|------|------|
| final_EV > θ_entry | Minimum edge (`θ_entry = 0.02 = 2¢`), **after VWAP** |
| order_allowed = True | NOT cooldown AND NOT ob_freeze |
| event_state == IDLE | Not during preliminary event state |
| liquidity_ok = True | Minimum order-book depth satisfied |
| engine_phase ∈ {FIRST_HALF, SECOND_HALF} | No entry during halftime/finished |
| alignment.status ≠ "DIVERGENT" (initially) | Block entry on divergence (Phase A) |

> **Filter relaxation by phase evolution:**
> - Phase A: block entry if DIVERGENT (conservative)
> - Phase B: allow DIVERGENT with multiplier 0.5 (data-driven from Step 4.6)
> - Phase C: tune multiplier if Step 4.6 shows positive divergent performance

### Signal Generation

```python
@dataclass
class Signal:
    direction: str              # BUY_YES, BUY_NO, HOLD
    EV: float                   # Final EV after VWAP
    P_cons: float               # Directional conservative P
    P_kalshi: float             # VWAP effective price
    rough_qty: int              # Rough quantity from Pass 1
    alignment_status: str       # ALIGNED, DIVERGENT, UNAVAILABLE
    kelly_multiplier: float     # 0.8, 0.5, 0.6
    market_ticker: str

def generate_signal(P_true, sigma_MC, ob_sync, P_bet365,
                    c, z, K_frac, bankroll, market_ticker) -> Signal:
    """2-pass VWAP + market alignment check"""

    # 2-pass VWAP computation
    base_signal = compute_signal_with_vwap(
        P_true, sigma_MC, ob_sync, c, z, K_frac, bankroll, market_ticker
    )

    if base_signal.direction == "HOLD":
        return base_signal

    # Market alignment check
    alignment = check_market_alignment(
        base_signal.P_cons, base_signal.P_kalshi, P_bet365, base_signal.direction
    )

    return Signal(
        direction=base_signal.direction,
        EV=base_signal.EV,
        P_cons=base_signal.P_cons,
        P_kalshi=base_signal.P_kalshi,
        rough_qty=base_signal.rough_qty,
        alignment_status=alignment.status,
        kelly_multiplier=alignment.kelly_multiplier,
        market_ticker=market_ticker
    )
```

### Output

```
Signal(direction, EV, P_cons, P_kalshi, rough_qty,
       alignment_status, kelly_multiplier, market_ticker)
```

---

## Step 4.3: Position Sizing — Fee-Adjusted Kelly Criterion

### Goal

Compute optimal investment fraction $f^*$ that maximizes long-run geometric growth
while keeping ruin probability at zero.

### Directional Fee-Adjusted Kelly

> Since `P_cons` is already adjusted by direction,
> Kelly should also use direction-specific win/loss (`W/L`) payoffs.

```python
def compute_kelly(signal: Signal, c: float, K_frac: float) -> float:
    """
    Kelly with directional P_cons + market alignment multiplier.
    P_kalshi is the VWAP effective price from Step 4.2.
    """
    P_cons = signal.P_cons
    P_kalshi = signal.P_kalshi  # VWAP effective price

    if signal.direction == "BUY_YES":
        # Yes win: profit (1 - P_kalshi), fee c applied
        # Yes loss: loss P_kalshi
        W = (1 - c) * (1 - P_kalshi)
        L = P_kalshi

    elif signal.direction == "BUY_NO":
        # No win (Yes=0): profit P_kalshi (= Yes sell price), fee c applied
        # No loss (Yes=100): loss (1 - P_kalshi)
        W = (1 - c) * P_kalshi
        L = (1 - P_kalshi)

    else:
        return 0.0

    if W * L <= 0:
        return 0.0

    f_kelly = signal.EV / (W * L)

    # Fractional Kelly
    f_invest = K_frac * f_kelly

    # Additional adjustment by market alignment
    f_invest *= signal.kelly_multiplier
    # ALIGNED → 0.8 (direction aligned with market, but limited independence)
    # DIVERGENT → 0.5 (direction conflict)
    # UNAVAILABLE → 0.6 (missing bet365 data)

    return max(0.0, f_invest)
```

### Fractional Kelly Policy

| K_frac | Growth (vs Full) | Volatility (vs Full) | Recommended Situation |
|--------|-------------------|-------------------|----------|
| 0.50 | 75% | 50% lower | Strong Brier score, 100+ trades accumulated |
| 0.25 | 44% | 75% lower | **Initial live stage (recommended starting point)** |

Never use Full Kelly (`K_frac = 1.0`).

### Correlated Position Cap Within Same Match

$$\sum_{\text{markets in match}} |f_{invest,i}| \leq f_{match\_cap}$$

If exceeded, scale proportionally:

$$f_{invest,i}^{scaled} = f_{invest,i} \times \frac{f_{match\_cap}}{\sum_i |f_{invest,i}|}$$

### 3-Layer Risk Limits

```python
def apply_risk_limits(f_invest: float, match_id: str,
                      bankroll: float) -> float:
    amount = f_invest * bankroll

    # Layer 1: single order ≤ 3%
    amount = min(amount, bankroll * F_ORDER_CAP)

    # Layer 2: per match ≤ 5%
    current_match_exposure = get_match_exposure(match_id)
    remaining_match = bankroll * F_MATCH_CAP - current_match_exposure
    amount = min(amount, max(0, remaining_match))

    # Layer 3: total portfolio ≤ 20%
    total_exposure = get_total_exposure()
    remaining_total = bankroll * F_TOTAL_CAP - total_exposure
    amount = min(amount, max(0, remaining_total))

    return amount
```

| Layer | Parameter | Default | Meaning |
|-------|---------|--------|------|
| 1 | f_order_cap | 0.03 (3%) | Single order cannot exceed 3% of capital |
| 2 | f_match_cap | 0.05 (5%) | Match exposure cannot exceed 5% |
| 3 | f_total_cap | 0.20 (20%) | Portfolio exposure cannot exceed 20% |

### Final Allocation Amount

$$\text{Amount}_i = \text{apply\_risk\_limits}(f_{invest,i}^{scaled}, \text{match\_id}, \text{Bankroll})$$

$$\text{Contracts}_i = \left\lfloor \frac{\text{Amount}_i}{P_{kalshi,i}} \right\rfloor$$

---

## Step 4.4: Position Exit Logic (Exit Signal)

### Goal

Close positions when edge decays or reverses due to changing in-match conditions.

### Four Exit Triggers

#### Trigger 1: Edge Decay

```python
def check_edge_decay(position, P_true, sigma_MC, P_kalshi_bid, c, z):
    if position.direction == "BUY_YES":
        P_cons = P_true - z * sigma_MC
    else:
        P_cons = P_true + z * sigma_MC

    current_EV = compute_position_EV(P_cons, P_kalshi_bid, position, c)
    if current_EV < THETA_EXIT:  # 0.005 = 0.5¢
        return ExitSignal(reason="EDGE_DECAY", EV=current_EV)
    return None
```

#### Trigger 2: Edge Reversal

> **[v2 fix #1] Buy No threshold: `(1 - P_kalshi_bid)` → `P_kalshi_bid`**

```python
def check_edge_reversal(position, P_true, sigma_MC, P_kalshi_bid, z):
    """
    Immediate exit if model now evaluates opposite to market.

    All comparisons are in Yes probability space.
    No (1 - P) conversion even for Buy No.
    """
    if position.direction == "BUY_YES":
        P_cons = P_true - z * sigma_MC
        # Reversal if model P(Yes) is θ below market P(Yes)
        if P_cons < P_kalshi_bid - THETA_ENTRY:
            return ExitSignal(reason="EDGE_REVERSAL")

    elif position.direction == "BUY_NO":
        P_cons = P_true + z * sigma_MC
        # [v2 fix] If model P(Yes) is θ above market P(Yes)
        # → model P(No) is lower than market → No position reversed
        if P_cons > P_kalshi_bid + THETA_ENTRY:
            return ExitSignal(reason="EDGE_REVERSAL")
        # ❌ previous: if P_cons > (1 - P_kalshi_bid) + THETA_ENTRY
        # with bid=0.40, required 0.62 → ~20pp too strict

    return None
```

> **Validation:**
> Buy No, `P_kalshi_bid = 0.40`, `θ = 0.02`
> - ❌ v1: `P_cons > (1 - 0.40) + 0.02 = 0.62` → reversal only at 62%
> - ✅ v2: `P_cons > 0.40 + 0.02 = 0.42` → reversal detected at 42%
>
> Buy No bets on “P(Yes) is low.”
> So if `P_cons(Yes)` exceeds market + θ, reversal is correct.

#### Trigger 3: Time-Based Expiry Evaluation (Last 3 Minutes)

> **[v2 fix #2] Added direction-specific `E_hold` for Buy No.**

```python
def check_expiry_eval(position, P_true, sigma_MC, P_kalshi_bid, c, z, t, T):
    """
    Near expiry: compare hold-to-settlement vs exit-now.
    E_hold differs by direction.
    """
    if T - t >= 3:
        return None

    if position.direction == "BUY_YES":
        P_cons = P_true - z * sigma_MC
    else:
        P_cons = P_true + z * sigma_MC

    # ─── E_hold: expected value if held to settlement ───
    if position.direction == "BUY_YES":
        # Yes win (prob P_cons): profit = (1 - entry) × (1-c)
        # Yes lose (prob 1-P_cons): loss = entry
        E_hold = (
            P_cons * (1 - c) * (1 - position.entry_price)
            - (1 - P_cons) * position.entry_price
        )

    elif position.direction == "BUY_NO":
        # [v2 fix] No win (prob 1-P_cons): profit = entry × (1-c)
        # No lose (prob P_cons): loss = (1 - entry)
        E_hold = (
            (1 - P_cons) * (1 - c) * position.entry_price
            - P_cons * (1 - position.entry_price)
        )
        # ❌ v1 reused Buy Yes formula → flips No expected value

    # ─── E_exit: expected value if exited now ───
    if position.direction == "BUY_YES":
        # sell Yes at bid
        profit_if_exit = P_kalshi_bid - position.entry_price
    elif position.direction == "BUY_NO":
        # close No = buy Yes at bid to offset
        # No entry sells Yes at entry_price → close buys Yes at P_kalshi_bid
        profit_if_exit = position.entry_price - P_kalshi_bid

    fee_if_exit = c * max(0, profit_if_exit)
    E_exit = profit_if_exit - fee_if_exit

    if E_exit > E_hold:
        return ExitSignal(reason="EXPIRY_EVAL", E_hold=E_hold, E_exit=E_exit)
    return None
```

> **Validation (Buy No):**
> `entry=0.40`, `P_cons=0.35`, `c=0.07`
>
> `E_hold = (1-0.35) × (1-0.07) × 0.40 - 0.35 × (1-0.40)`
> `       = 0.65 × 0.93 × 0.40 - 0.35 × 0.60`
> `       = 0.2418 - 0.21 = +0.0318` (holding is better)
>
> ❌ v1 (Buy Yes formula reused):
> `E_hold = 0.35 × 0.93 × 0.60 - 0.65 × 0.40`
> `       = 0.1953 - 0.26 = -0.0647` (wrongly favors exit)

#### Trigger 4: bet365 Divergence Warning

> **[v2 fix #3] Buy No threshold: `(1 - entry_price)` → `entry_price`**

```python
def check_bet365_divergence(position, P_bet365: float) -> Optional[DivergenceAlert]:
    """
    Warning when bet365 moves against held position direction.
    All comparisons are in Yes probability space.
    """
    if P_bet365 is None:
        return None

    DIVERGENCE_THRESHOLD = 0.05  # 5pp

    if position.direction == "BUY_YES":
        # Yes held: warning if bet365 P(Yes) drops by 5pp below entry
        if P_bet365 < position.entry_price - DIVERGENCE_THRESHOLD:
            return DivergenceAlert(
                severity="WARNING",
                P_bet365=P_bet365,
                P_entry=position.entry_price,
                suggested_action="REDUCE_OR_EXIT"
            )

    elif position.direction == "BUY_NO":
        # [v2 fix] No held (= sold Yes):
        # warning if bet365 P(Yes) rises by 5pp above entry
        # (Yes up is adverse for No position)
        if P_bet365 > position.entry_price + DIVERGENCE_THRESHOLD:
            return DivergenceAlert(
                severity="WARNING",
                P_bet365=P_bet365,
                P_entry=position.entry_price,
                suggested_action="REDUCE_OR_EXIT"
            )
        # ❌ v1: if P_bet365 > (1 - position.entry_price) + 0.05
        # entry=0.40 => v1 needs 0.65 (25pp), v2 needs 0.45 (5pp)

    return None
```

> **Validation (Buy No):**
> `entry=0.40` (sold Yes at 0.40)
> - ❌ v1: `P_bet365 > (1-0.40)+0.05 = 0.65` → needs 25pp move
> - ✅ v2: `P_bet365 > 0.40+0.05 = 0.45` → warns at 5pp (symmetric with Buy Yes)

**Trigger 4 is logging-only initially.**
Enable auto-exit after enough data in Step 4.6.

### Full Exit Evaluation Loop

```python
async def evaluate_exit(position, P_true, sigma_MC, P_kalshi_bid,
                        P_bet365, c, z, t, T) -> Optional[ExitSignal]:
    """Call this each tick for all open positions"""

    # Trigger 1: edge decay
    exit = check_edge_decay(position, P_true, sigma_MC, P_kalshi_bid, c, z)
    if exit: return exit

    # Trigger 2: edge reversal
    exit = check_edge_reversal(position, P_true, sigma_MC, P_kalshi_bid, z)
    if exit: return exit

    # Trigger 3: expiry eval
    exit = check_expiry_eval(position, P_true, sigma_MC, P_kalshi_bid, c, z, t, T)
    if exit: return exit

    # Trigger 4: bet365 divergence warning
    divergence = check_bet365_divergence(position, P_bet365)
    if divergence:
        log.warning(f"bet365 divergence: {divergence}")
        position.had_bet365_divergence = True
        position.divergence_snapshot = {
            "P_bet365": P_bet365,
            "P_kalshi_bid": P_kalshi_bid,
            "P_true": P_true,
            "t": t,
        }
        if BET365_DIVERGENCE_AUTO_EXIT:
            return ExitSignal(reason="BET365_DIVERGENCE")

    return None
```

---

## Step 4.5: Order Execution & Risk Management

### Order Types

| Situation | Order Type | Reason |
|------|----------|------|
| Normal entry | Limit Order (Ask + 0~1¢) | Balance fill probability and slippage |
| Urgent exit | Limit Order (Bid - 1¢) | Prioritize quick fill |
| **Rapid Entry** | **Limit Order (Ask + 1¢)** | **Post-event informational edge (conditional)** |
| Low liquidity | Hold order | If slippage > edge, no entry |

### Order Submission

```python
async def execute_order(signal: Signal, amount: float,
                        ob_sync: OrderBookSync,
                        urgent: bool = False) -> Optional[FillResult]:
    P_kalshi = signal.P_kalshi  # VWAP effective price
    contracts = int(amount / P_kalshi)

    if contracts < 1:
        return None

    if urgent:
        price_cents = int(ob_sync.kalshi_best_ask * 100) + 1
    else:
        price_cents = int(ob_sync.kalshi_best_ask * 100)

    order = {
        "ticker": signal.market_ticker,
        "action": "buy",
        "side": "yes" if signal.direction == "BUY_YES" else "no",
        "type": "limit",
        "count": contracts,
        "yes_price": price_cents if signal.direction == "BUY_YES"
                     else (100 - price_cents),
    }

    response = await kalshi_api.submit_order(order)
    order_id = response["order"]["id"]

    filled = await wait_for_fill(order_id, timeout=5)

    if filled.status == "full":
        record_position(signal, filled)
        return filled
    elif filled.status == "partial":
        await kalshi_api.cancel_order(order_id)
        record_position(signal, filled, partial=True)
        return filled
    else:
        await kalshi_api.cancel_order(order_id)
        return None
```

### Paper Fill Simulation

> **[v2 fix #6] VWAP + slippage + partial-fill simulation.**

```python
class PaperExecutionLayer:
    def __init__(self, slippage_ticks: int = 1):
        self.slippage_ticks = slippage_ticks

    async def execute_order(self, signal: Signal, amount: float,
                            ob_sync: OrderBookSync,
                            urgent: bool = False) -> Optional[PaperFill]:
        """
        Paper fill simulation:
        1. VWAP-based fill price (includes book depth)
        2. Add slippage (1~2 ticks)
        3. Partial fill simulation (if order exceeds available depth)

        ❌ v1: full instant fill at best ask → optimistic bias
        ✅ v2: realistic fill simulation
        """
        target_qty = int(amount / signal.P_kalshi)
        if target_qty < 1:
            return None

        # VWAP effective price
        if signal.direction == "BUY_YES":
            P_effective = ob_sync.compute_vwap_buy(target_qty)
        else:
            P_effective = ob_sync.compute_vwap_sell(target_qty)

        if P_effective is None:
            return None  # insufficient depth

        # Add slippage
        fill_price = P_effective + (self.slippage_ticks * 0.01)

        # Partial fill based on available depth
        if signal.direction == "BUY_YES":
            available_depth = sum(qty for _, qty in ob_sync.kalshi_depth_ask
                                 if _ <= fill_price * 100)
        else:
            available_depth = sum(qty for _, qty in ob_sync.kalshi_depth_bid
                                 if _ >= fill_price * 100)

        filled_qty = min(target_qty, available_depth)
        if filled_qty < 1:
            return None

        return PaperFill(
            price=fill_price,
            quantity=filled_qty,
            timestamp=time.time(),
            is_paper=True,
            slippage=fill_price - ob_sync.kalshi_best_ask,
            partial=(filled_qty < target_qty)
        )
```

### Rapid Entry

> **[v2 fix #7] VAR safety wait + conservative P_cons + stricter activation conditions.**

```python
async def post_event_rapid_entry(model, confirmed_event):
    """
    Evaluate immediate post-confirmation entry before cooldown.
    """
    if not RAPID_ENTRY_ENABLED:
        return

    # [v2] VAR safety wait: extra N seconds after CONFIRMED
    # If no score rollback occurs during this period, treat as safe
    await asyncio.sleep(VAR_SAFETY_WAIT)  # default 5s

    # Recheck state after waiting
    if model.event_state != "IDLE":
        return  # new event occurred — abort
    if model.S != confirmed_event.score:
        return  # score changed — possible VAR cancellation

    # Use precomputed P_true
    if not model.preliminary_cache.get("μ_H"):
        return

    P_true = compute_P_from_preliminary(model)
    sigma_MC = model.preliminary_cache.get("sigma_MC", 0.01)

    # [v2] conservative P_cons adjustment (v1 used P_cons=P_true)
    direction = infer_direction(P_true, model.ob_sync.kalshi_best_ask)
    P_cons = compute_conservative_P(P_true, sigma_MC, direction, model.config.z)

    P_bet365 = model.ob_sync.bet365_implied.get(market_key)
    P_kalshi = model.ob_sync.kalshi_best_ask

    if P_bet365 is None or P_kalshi is None:
        return

    # Market alignment check
    alignment = check_market_alignment(P_cons, P_kalshi, P_bet365, direction)

    if alignment.status == "ALIGNED":
        # VWAP-based EV
        rough_qty = estimate_rapid_qty(P_cons, P_kalshi, model)
        P_effective = model.ob_sync.compute_vwap_buy(rough_qty) if direction == "BUY_YES" \
                      else model.ob_sync.compute_vwap_sell(rough_qty)

        if P_effective is None:
            return

        if direction == "BUY_YES":
            EV = P_cons * (1-c) * (1-P_effective) - (1-P_cons) * P_effective
        else:
            EV = (1-P_cons) * (1-c) * P_effective - P_cons * (1-P_effective)

        if EV <= THETA_ENTRY:
            return

        signal = Signal(
            direction=direction, EV=EV, P_cons=P_cons,
            P_kalshi=P_effective, rough_qty=rough_qty,
            alignment_status="ALIGNED", kelly_multiplier=0.8,
            market_ticker=model.active_market
        )
        amount = compute_kelly(signal, c, K_frac)
        amount = apply_risk_limits(amount, model.match_id, model.bankroll)
        await model.execution.execute_order(signal, amount, model.ob_sync, urgent=True)
        log.info(f"RAPID ENTRY: {signal.direction}, EV={signal.EV:.4f}")
```

**Rapid Entry activation conditions (strengthened):**

```python
RAPID_ENTRY_ENABLED = (
    cumulative_trades >= 200
    and edge_realization >= 0.8
    and preliminary_accuracy >= 0.95
    and var_cancellation_rate < 0.03
    and VAR_SAFETY_WAIT >= 5              # [v2] safety wait is configured
    and rapid_entry_hypo_pnl_after_slip > 0  # [v2] remains positive after slippage
)
```

### Trade Log

```python
@dataclass
class TradeLog:
    timestamp: float
    match_id: str
    market_ticker: str
    direction: str              # BUY_YES | BUY_NO | SELL_YES | SELL_NO
    order_type: str             # ENTRY | EXIT_EDGE_DECAY | EXIT_EDGE_REVERSAL
                                # | EXIT_EXPIRY_EVAL | EXIT_BET365_DIVERGENCE
                                # | RAPID_ENTRY
    quantity_ordered: int
    quantity_filled: int
    limit_price: float
    fill_price: float
    P_true_at_order: float
    P_true_cons_at_order: float     # Directional conservative P
    P_kalshi_at_order: float        # VWAP effective price
    P_kalshi_best_at_order: float   # Best ask/bid (for VWAP comparison)
    P_bet365_at_order: float
    EV_adj: float                   # Final EV after VWAP
    sigma_MC: float
    pricing_mode: str
    f_kelly: float
    K_frac: float
    alignment_status: str           # ALIGNED | DIVERGENT | UNAVAILABLE
    kelly_multiplier: float
    cooldown_active: bool
    ob_freeze_active: bool
    event_state: str
    engine_phase: str
    bankroll_before: float
    bankroll_after: float
    is_paper: bool
    paper_slippage: float           # In paper mode: simulated slippage
```

---

## Step 4.6: Post-Match Settlement and Analysis

### Auto-Settlement

> **[v2 fix #8] Added directional settlement branch for Buy No.**

```python
def compute_realized_pnl(position, settlement_price: float,
                          fee_rate: float) -> float:
    """
    Direction-specific realized P&L.

    settlement_price: settlement from Yes perspective (Yes win=1.00, Yes lose=0.00)

    Buy Yes: profit = Settlement - Entry (Yes at 100¢ is profit)
    Buy No:  profit = Entry - Settlement (Yes at 0¢ is profit)

    ❌ v1: Qty × (Settlement - Entry) - Fee → sign flips for Buy No
    ✅ v2: directional branch
    """
    if position.direction == "BUY_YES":
        gross_pnl = (settlement_price - position.entry_price) * position.quantity
    elif position.direction == "BUY_NO":
        gross_pnl = (position.entry_price - settlement_price) * position.quantity
    else:
        gross_pnl = 0

    # Fee applies only to profits
    fee = fee_rate * max(0, gross_pnl)
    return gross_pnl - fee
```

> **Validation:**
>
> | Direction | Entry | Settlement | v1 Result | v2 Result | Actual |
> |------|-------|------------|---------|---------|------|
> | Buy Yes | 0.45 | 1.00 | +0.55 ✅ | +0.55 ✅ | Profit |
> | Buy Yes | 0.45 | 0.00 | -0.45 ✅ | -0.45 ✅ | Loss |
> | Buy No | 0.40 | 0.00 | -0.40 ❌ | +0.40 ✅ | Profit (No wins) |
> | Buy No | 0.40 | 1.00 | +0.60 ❌ | -0.60 ✅ | Loss (No loses) |
>
> In v1, Buy No profit/loss is completely inverted.
> That contaminates all Step 4.6 post-analysis metrics (Brier, edge realization, drawdown, etc.).

### Post-Analysis Metrics — 11 Total

#### Original metrics (1~6)

**1. Match-level P&L:**

$$\text{Match P\&L} = \sum_{i \in \text{positions}} \text{compute\_realized\_pnl}(i)$$

**2. Cumulative Brier Score** (vs Pinnacle baseline)

**3. Edge Realization:**

$$\text{Edge Realization} = \frac{\text{Actual average return}}{\text{Expected average } EV_{adj}}$$

**4. Slippage Performance:**

$$\text{Avg Slippage} = \frac{1}{N}\sum_{n} (\text{Fill Price}_n - P_{kalshi,best,n})$$

> Track difference between `P_kalshi_best_at_order` (best ask/bid) and actual fill.
> Since VWAP is in EV, also track slippage between VWAP and actual fill.

**5. Cooldown impact analysis**

**6. ob_freeze impact analysis**

#### New metrics (7~11)

**7. Market alignment value:**

```python
def analyze_alignment_effect(trades):
    aligned = [t for t in trades if t.alignment_status == "ALIGNED"]
    divergent = [t for t in trades if t.alignment_status == "DIVERGENT"]

    return {
        "aligned_avg_return": safe_mean([t.realized_pnl for t in aligned]),
        "divergent_avg_return": safe_mean([t.realized_pnl for t in divergent]),
        "aligned_win_rate": win_rate(aligned),
        "divergent_win_rate": win_rate(divergent),
        "alignment_value": (
            safe_mean([t.realized_pnl for t in aligned])
            - safe_mean([t.realized_pnl for t in divergent])
        ),
    }
```

**8. Directional P_true^cons analysis:**

```python
def analyze_directional_cons(trades):
    yes = [t for t in trades if t.direction == "BUY_YES"]
    no = [t for t in trades if t.direction == "BUY_NO"]

    return {
        "yes_edge_realization": safe_divide(actual_return(yes), expected_EV(yes)),
        "no_edge_realization": safe_divide(actual_return(no), expected_EV(no)),
    }
```

**9. Preliminary accuracy**

**10. Rapid Entry hypothetical P&L:**

> [v2] Include VWAP + slippage in hypothetical P&L for realism.

**11. bet365 divergence warning effectiveness**

### Model Health Dashboard

| Metric | Healthy 🟢 | Warning 🟡 | Risk 🔴 |
|------|---------|---------|---------|
| Brier Score | Phase 1.5 ± 0.02 | ± 0.05 | outside band |
| Edge Realization | 0.7~1.3 | 0.5~0.7 | < 0.5 |
| Max Drawdown | < 10% | 10~20% | > 20% |
| Market alignment value | ALIGNED > DIVERGENT + 1¢ | gap ≈ 0 | ALIGNED < DIVERGENT |
| Preliminary accuracy | > 0.95 | 0.90~0.95 | < 0.90 |
| No-side realization | 0.7~1.3 | > 1.5 (too conservative) | < 0.5 |

### Feedback Loop — Adaptive Parameter Tuning

```python
def adaptive_parameter_update(analytics: dict):
    """Data-driven auto-adjustment of 7 parameters"""

    # 1. K_frac adjustment
    er = analytics["edge_realization"]
    if er >= 0.8:
        K_frac = min(K_frac + 0.05, 0.50)
    elif er < 0.5:
        K_frac = max(K_frac - 0.10, 0.10)

    # 2. Market alignment multiplier adjustment
    av = analytics["alignment_value"]
    if av < 0.005:
        # low alignment value → raise DIVERGENT multiplier
        DIVERGENT_MULTIPLIER = 0.65
    elif av > 0.015:
        DIVERGENT_MULTIPLIER = 0.4

    # 3. Rapid entry activation decision
    if (analytics["preliminary_accuracy"] > 0.95
        and analytics["var_cancellation_rate"] < 0.03
        and analytics["rapid_entry_hypo_pnl_after_slip"] > 0
        and analytics["cumulative_trades"] >= 200):
        RAPID_ENTRY_ENABLED = True

    # 4. z (conservativeness) adjustment — directional
    no_er = analytics["no_edge_realization"]
    if no_er > 1.5:
        z = max(z - 0.2, 1.0)
    elif no_er < 0.5:
        z = min(z + 0.2, 2.0)

    # 5. Phase 1 retraining trigger
    if analytics["brier_score_trend"] == "worsening_3weeks":
        trigger_phase1_recalibration()

    # 6. Cooldown adjustment
    if analytics["cooldown_suppressed_profitable_rate"] > 0.6:
        COOLDOWN_SECONDS = max(COOLDOWN_SECONDS - 2, 8)

    # 7. bet365 divergence auto-exit decision
    if (analytics["bet365_divergence_should_auto_exit"]
        and analytics["bet365_divergence_sample_size"] >= 30):
        BET365_DIVERGENCE_AUTO_EXIT = True
```

---

## Phase 4 Pipeline Summary (v2)

```
[Phase 3: P_true, σ_MC, order_allowed, event_state, P_bet365]
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│  Step 4.1: Order-Book Sync                                  │
│  • Kalshi WS → Bid/Ask + VWAP buy/sell effective prices    │
│  • Goalserve Live Odds WS → bet365 implied probabilities   │
│  • Liquidity filter (Q_min ≥ 20 contracts)                 │
│  Output: P_kalshi^buy, P_kalshi^sell,                      │
│          P_effective^buy(Q), P_effective^sell(Q), [v2 VWAP]│
│          P_bet365, liquidity                               │
└──────────────────┬──────────────────────────────────────────┘
                   ▼
┌─────────────────────────────────────────────────────────────┐
│  Step 4.2: Edge Detection                                   │
│  • Directional P_cons: Yes→P-zσ, No→P+zσ                   │
│  • 2-pass VWAP EV calc:                             [v2]    │
│    Pass 1: best ask/bid → rough qty                        │
│    Pass 2: VWAP for rough qty → final EV                   │
│  • Market alignment check:                         [v2]     │
│    → ALIGNED (mult 0.8) / DIVERGENT (0.5)                  │
│       / UNAVAILABLE (0.6)                                  │
│  • Filter: EV>θ (after VWAP) AND order_allowed             │
│          AND event_state==IDLE AND liquidity_ok            │
│          AND alignment policy by phase                     │
│  Output: Signal(direction, EV, P_cons,                     │
│          P_kalshi=VWAP, alignment_status)                  │
└──────────────────┬──────────────────────────────────────────┘
                   │
         ┌─────────┴─────────┐
         │ Entry Signal       │ Existing Position
         ▼                    ▼
┌──────────────────┐  ┌──────────────────────────────────────┐
│  Step 4.3:       │  │  Step 4.4: Exit (directional formulas)│
│  Sizing          │  │                                  [v2]│
│                  │  │  Trigger 1: Edge decay (EV < 0.5¢)    │
│  • Directional   │  │  Trigger 2: Edge reversal             │
│    Kelly (W/L)   │  │    Yes: P_cons < P_bid - θ            │
│  • K_frac        │  │    No:  P_cons > P_bid + θ     [v2]   │
│    (0.25~0.50)   │  │  Trigger 3: Expiry eval (last 3 min)  │
│  • Alignment     │  │    Directional E_hold branch   [v2]   │
│    multiplier    │  │  Trigger 4: bet365 divergence warning │
│    (0.8/0.5/0.6) │  │    Yes: P_bet365 < entry - 5pp        │
│      [v2]        │  │    No:  P_bet365 > entry + 5pp [v2]   │
│  • Match cap     │  │       → logging first, then optional   │
│    pro-rata      │  │         auto-exit after data           │
│  • 3-layer risk  │  │                                        │
│    (3%/5%/20%)   │  │                                        │
└────────┬─────────┘  └──────────┬─────────────────────────────┘
         │                       │
         └───────────┬───────────┘
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  Step 4.5: Order Execution                                  │
│                                                             │
│  • Normal entry: Limit Order (Ask + 0~1¢)                  │
│  • Urgent exit: Limit Order (Bid - 1¢)                     │
│  • Rapid Entry: Ask + 1¢ (conditional)                     │
│    + 5s VAR safety wait                            [v2]     │
│    + P_cons z-adjustment                           [v2]     │
│  • Partial fill: 5s timeout → cancel unfilled remainder    │
│                                                             │
│  Paper mode:                                        [v2]    │
│  • VWAP fill + 1 tick slippage + partial fills             │
│                                                             │
│  Trade log: P_true, P_cons, P_kalshi(VWAP),                │
│    P_kalshi_best, P_bet365, alignment_status,      [v2]    │
│    kelly_multiplier, event_state, paper_slippage           │
│                                                             │
│  Real-time feedback: position DB + bankroll + risk refresh │
└──────────────────┬──────────────────────────────────────────┘
                   ▼ (after match end)
┌─────────────────────────────────────────────────────────────┐
│  Step 4.6: Settlement & Post-Analysis                       │
│                                                             │
│  Settlement (directional):                           [v2]   │
│  • Buy Yes: (Settlement - Entry) × Qty - Fee              │
│  • Buy No:  (Entry - Settlement) × Qty - Fee              │
│                                                             │
│  Original metrics (1~6):                                    │
│  1. Match-level P&L (directional settlement)        [v2]    │
│  2. Brier Score (vs Pinnacle baseline)                     │
│  3. Edge realization                                        │
│  4. Slippage performance (adds VWAP-vs-fill view)   [v2]    │
│  5. Cooldown effect                                         │
│  6. ob_freeze effect                                        │
│                                                             │
│  New metrics (7~11):                                        │
│  7. Market alignment value                           [v2]    │
│     (ALIGNED vs DIVERGENT return gap)                      │
│  8. Directional P_cons analysis (Yes vs No realization)     │
│  9. Preliminary accuracy (for rapid entry decisions)        │
│ 10. Rapid Entry hypothetical P&L (slippage-adjusted) [v2]   │
│ 11. bet365 divergence warning value (auto-exit decision)    │
│                                                             │
│  Adaptive tuning (7 parameters):                            │
│  1. K_frac (0.25~0.50)                                      │
│  2. DIVERGENT multiplier                            [v2]    │
│  3. Rapid entry on/off                                      │
│  4. z (directional conservativeness)                        │
│  5. Phase 1 retraining trigger                              │
│  6. Cooldown length (15s~8s)                                │
│  7. bet365 divergence auto-exit on/off                      │
│                                                             │
│  System evolution: Phase 0 → A → B → C roadmap             │
│                                                             │
│  Output: P&L report, health dashboard, parameter updates,   │
│          retraining decisions                                │
└─────────────────────────────────────────────────────────────┘
              │
              ▼
   [Phase 1 retraining (when triggered)]
```

---

## System Evolution Roadmap

```
Phase 0 — Paper Trading:
│  • K_frac = 0.25, z = 1.645
│  • Paper fills: VWAP + 1 tick slippage [v2]
│  • Block entry on DIVERGENT
│  • Rapid entry disabled
│  Period: 2~4 weeks
│
▼
Phase A — Conservative Live:
│  • Keep blocking DIVERGENT entries
│  • Rapid entry disabled
│  Period: 1~2 months
│
▼
Phase B — Adaptive Live:
│  • K_frac → 0.25~0.50 (based on Step 4.6)
│  • DIVERGENT entries allowed with multiplier 0.5
│  • Directional optimization of z
│  Period: 2~4 months
│
▼
Phase C — Mature Live:
│  • Conditional rapid entry enabled (with VAR safety wait) [v2]
│  • bet365 divergence → auto-exit (if supported by data)
│  • Auto-tuning loop enabled
│
▼
(Every season: mandatory Phase 1 retraining)
```

---

## Full System Feedback Loop

```
Phase 1 (Offline Calibration)
│  Parameters: b[], γ^H, γ^A, δ_H, δ_A, Q, XGBoost weights
│
▼
Phase 2 (Pre-Match Initialization)
│  Initialize: a_H, a_A, P_grid, Q_off_normalized, C_time, T_exp
│
▼
Phase 3 (Live Trading Engine)
│  Real-time: P_true(t), σ_MC(t), order_allowed, P_bet365(t)
│  3-Layer: Live Odds WS + Kalshi WS + Live Score REST
│
▼
Phase 4 (Arbitrage & Execution) [v2]
│  • VWAP-connected EV (2-pass)
│  • Directional P_cons (Yes→lower bound, No→upper bound)
│  • Directional Kelly W/L
│  • Directional exit triggers (edge reversal, expiry, settlement)
│  • Market alignment check (not independent validation, multiplier 0.8)
│  • Paper: VWAP + slippage + partial fill
│  • Rapid Entry: VAR safety wait + P_cons adjustment
│
▼
Step 4.6 (Post-Match Analytics)
│  Analysis: 11 metrics
│  Tuning: 7 parameters
│
└──▶ Phase 1 retraining (when triggered)
```

---

## v2 Change Tracking

| # | Location | Before | After |
|---|------|--------|--------|
| 1 | Step 4.4 Trigger 2 | `P_cons > (1-P_bid) + θ` | `P_cons > P_bid + θ` |
| 2 | Step 4.4 Trigger 3 | Only Buy Yes `E_hold` | Directional `E_hold` branch |
| 3 | Step 4.4 Trigger 4 | `P_bet365 > (1-entry) + 0.05` | `P_bet365 > entry + 0.05` |
| 4 | Step 4.1~4.2 | “Independent validation”, mult 1.0 | “Market alignment”, mult 0.8 |
| 5 | Step 4.2 | EV with best ask/bid | EV with 2-pass VWAP |
| 6 | Step 4.5 Paper | Full instant fill at best ask | VWAP + 1 tick + partial fill |
| 7 | Step 4.5 Rapid | No VAR wait, no P_cons adjustment | 5s wait + z-adjustment + stricter conditions |
| 8 | Step 4.6 settlement | `Qty × (Sett - Entry)` | Directional `BuyYes: Sett-Entry`, `BuyNo: Entry-Sett` |
