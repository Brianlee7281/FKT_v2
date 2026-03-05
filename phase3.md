# Phase 3: Live Trading Engine — Goalserve Full Package

## Overview

A dynamic pricing engine that runs every second from kickoff to full time.

As option time value decays over time (Theta Decay),
it continuously decays expected goals by remaining match time,
while instantly re-adjusting the probability space whenever
jump events such as goals or red cards occur.

Using parameters learned in Phase 1 and initial conditions set in Phase 2,
it repeats the cycle of
**remaining expected goals μ calculation -> true probability P_true estimation**
every second.

This process is decomposed into five steps.

### Architecture Paradigm Shift: 3-Layer Detection

Goalserve full package Live Odds WebSocket (<1s)
fundamentally changes the Phase 3 architecture.

**Original design (2-layer):**
```
Defense 1: Goalserve Live Score REST (3-8s) -> event confirmation
Defense 2: Kalshi quote spike (1-2s) -> ob_freeze
```

**Full package (3-layer):**
```
Defense 1: Goalserve Live Odds WebSocket (<1s) -> first detection
Defense 2: Kalshi quote spike (1-2s) -> cross-check
Defense 3: Goalserve Live Score REST (3-8s) -> authoritative confirmation
```

Role by event source:

| Source | Protocol | Latency | Data Provided | Role |
|------|---------|------|-----------|------|
| **Goalserve Live Odds** | **WebSocket PUSH** | **<1s** | score, minute, period, bet365 odds, ball position, game state | **primary detection + ob_freeze** |
| Kalshi API | WebSocket | 1-2s | order book | cross-check + execution |
| **Goalserve Live Score** | **REST polling every 3s** | 3-8s | goal scorer, card detail, substitutions, VAR | **authoritative confirmation + details** |

Core insight:
- **Live Odds** tells us **first** that "something happened" (score change, abrupt odds move)
- **Live Score** tells us "what exactly happened" (who scored, whether VAR cancelled)

Why both are required: Live Odds can show score changing 1-0 -> 1-1,
but only Live Score can confirm whether it was a regular goal, own goal,
or later VAR cancellation.

### Intensity Function (Fixed in Phase 1)

$$\lambda_H(t \mid X, \Delta S) = \exp\!\left(a_H + b_{i(t)} + \gamma^H_{X(t)} + \delta_H(\Delta S(t))\right)$$

$$\lambda_A(t \mid X, \Delta S) = \exp\!\left(a_A + b_{i(t)} + \gamma^A_{X(t)} + \delta_A(\Delta S(t))\right)$$

| Symbol | Meaning | Change Trigger |
|------|------|-----------|
| $a_H, a_A$ | match-level baseline intensity | invariant during match |
| $b_{i(t)}$ | time-interval profile | when basis boundary is crossed |
| $\gamma^H_{X(t)}$ | red card -> home penalty | on red card (jump) |
| $\gamma^A_{X(t)}$ | red card -> away penalty | on red card (jump) |
| $\delta_H(\Delta S)$ | score-diff home tactical effect | on goal (jump) |
| $\delta_A(\Delta S)$ | score-diff away tactical effect | on goal (jump) |

---

## Input Data

**Phase 2 outputs:**

| Item | Usage |
|------|------|
| `LiveFootballQuantModel` instance | all parameters + initial state |
| $P_{grid}[0..100]$ + $P_{fine\_grid}$ | precomputed matrix exponentials |
| $Q_{off\_normalized}$ (4x4) | normalized transition probabilities for MC |
| $C_{time}$, $T_{exp}$ | time constants |
| `DELTA_SIGNIFICANT` | analytic/MC mode-selection flag |

**Real-time data streams (3 sources):**

| Source | Goalserve Endpoint | Data |
|------|---------------------|--------|
| Live Odds WS | `wss://goalserve.com/liveodds/{api_key}/{match_id}` | score, bet365 odds, minute, status, ball position |
| Live Score REST | `GET /getfeed/{api_key}/soccerlive/home?json=1` | goal scorer, cards, substitutions, VAR |
| Kalshi WS | Kalshi WebSocket API | order book (Bid/Ask + depth) |

---

## Step 3.1: Asynchronous Real-Time Data Ingestion and State Machine (Event Loop & State Machine)

### Goal

Track physical time (seconds) and match state simultaneously,
and implement a 3-layer event detection framework with two-stage preliminary -> confirmed handling.

### Engine State Machine (Engine Phase)

```
FIRST_HALF --(first-half end)--> HALFTIME --(second-half kickoff)--> SECOND_HALF --(full time)--> FINISHED
```

| Engine State | Time Range | Pricing | Orders |
|----------|----------|---------|------|
| `FIRST_HALF` | $[0,\; 45+\alpha_1]$ | active | active |
| `HALFTIME` | about 15 min | **frozen** | **stopped** |
| `SECOND_HALF` | $[45+\alpha_1+\delta_{HT},\; T_m]$ | active | active |
| `FINISHED` | — | final settlement | — |

Since lambda(t) = 0 during halftime, continuing pricing creates
fictional decay where "time passes but no goals can occur."
In `HALFTIME`, freeze both pricing and orders.

### Event State Machine (Event State)

Event handling states for 3-layer detection:

```
IDLE --(Live Odds score change)--> PRELIMINARY_DETECTED
  |                                    |
  |                               ob_freeze = True
  |                               μ pre-compute (provisional)
  |                                    |
  |                    +---------------+---------------+
  |                    v                               v
  |              CONFIRMED                       FALSE_ALARM
  |            (Live Score confirms)      (3-tick stabilization or 10s timeout)
  |                    |                               |
  |              +-----+-----+                    ob_freeze = False
  |              v           v                    keep state
  |       not VAR-cancelled  VAR-cancelled             |
  |              |           |                         |
  |         commit S,ΔS,X    rollback                  |
  |         cooldown 15s     ob_freeze = False         |
  |         ob_freeze=F           |                    |
  |              |                |                    |
  +--------------+----------------+--------------------+
                 v
               IDLE (return)
```

### Mathematical State Variables

```
t           : current effective play time (halftime excluded)
S(t)        : current score (S_H, S_A)
X(t)        : Markov state ∈ {0, 1, 2, 3}
ΔS(t)       : current score difference = S_H - S_A
engine_phase: {FIRST_HALF, HALFTIME, SECOND_HALF, FINISHED}
event_state : {IDLE, PRELIMINARY_DETECTED, CONFIRMED}
cooldown    : bool (15s order block after event)
ob_freeze   : bool (order block on anomaly detection)
T           : currently applied expected match end time
```

### EventSource Abstraction

```python
class EventSource(ABC):
    """Abstract layer decoupling engine and data sources."""
    async def connect(self, match_id: str) -> None: ...
    async def listen(self) -> AsyncIterator[NormalizedEvent]: ...
    async def disconnect(self) -> None: ...

@dataclass
class NormalizedEvent:
    type: str           # goal_detected, goal_confirmed, red_card,
                        # period_change, odds_spike, stoppage_entered
    source: str         # "live_odds" or "live_score"
    confidence: str     # "preliminary" or "confirmed"
    timestamp: float
    # Additional event-specific fields
    score: Optional[Tuple[int, int]] = None
    team: Optional[str] = None
    minute: Optional[float] = None
    period: Optional[str] = None
    var_cancelled: Optional[bool] = None
    scorer_id: Optional[str] = None
    delta: Optional[float] = None
```

### Source 1: Goalserve Live Odds WebSocket (Primary Detection, <1s)

```python
class GoalserveLiveOddsSource(EventSource):
    """
    WebSocket PUSH - <1s latency.
    bet365 in-play odds + match info (score, minute, status).

    Goalserve Live Odds response format:
    {
      "info": {
        "score": "0:0",
        "minute": "45",
        "period": "Paused",
        "ball_pos": "x23;y46",
        "state": "1015"
      },
      "markets": {
        "1777": {  # Fulltime Result
          "participants": {
            "2009353051": {"name": "Home", "value_eu": "1.44", ...},
            "2009353052": {"name": "Draw", "value_eu": "3.50", ...},
            "2009353054": {"name": "Away", "value_eu": "12.00", ...}
          }
        }
      }
    }
    """

    def __init__(self, odds_threshold_pct: float = 0.10):
        self.ODDS_THRESHOLD = odds_threshold_pct
        self._last_score = None
        self._last_period = None
        self._last_home_odds = None
        self._stoppage_entered = {"first": False, "second": False}
        self._ball_pos = ""
        self._game_state = ""

    async def listen(self) -> AsyncIterator[NormalizedEvent]:
        async for msg in self.ws:
            parsed = json.loads(msg)
            info = parsed["info"]

            # --- Score change detection ---
            new_score = self._parse_score(info["score"])
            if self._last_score is not None and new_score != self._last_score:

                # Score decreases? -> potential VAR cancellation
                if (new_score[0] < self._last_score[0] or
                    new_score[1] < self._last_score[1]):
                    yield NormalizedEvent(
                        type="score_rollback",
                        source="live_odds",
                        confidence="preliminary",
                        score=new_score,
                        timestamp=time.time()
                    )
                else:
                    yield NormalizedEvent(
                        type="goal_detected",
                        source="live_odds",
                        confidence="preliminary",
                        score=new_score,
                        timestamp=time.time()
                    )
            self._last_score = new_score

            # --- Period change detection ---
            new_period = info.get("period", "")
            if new_period and new_period != self._last_period:
                yield NormalizedEvent(
                    type="period_change",
                    source="live_odds",
                    confidence="preliminary",
                    period=new_period,
                    minute=self._parse_minute(info.get("minute", "")),
                    timestamp=time.time()
                )
                self._last_period = new_period

            # --- Stoppage-time entry detection ---
            minute = self._parse_minute(info.get("minute", ""))
            if minute:
                period = info.get("period", "")
                if period in ("1st Half", "1st") and minute > 45:
                    if not self._stoppage_entered["first"]:
                        self._stoppage_entered["first"] = True
                        yield NormalizedEvent(
                            type="stoppage_entered",
                            source="live_odds",
                            confidence="preliminary",
                            period="first",
                            minute=minute,
                            timestamp=time.time()
                        )
                elif period in ("2nd Half", "2nd") and minute > 90:
                    if not self._stoppage_entered["second"]:
                        self._stoppage_entered["second"] = True
                        yield NormalizedEvent(
                            type="stoppage_entered",
                            source="live_odds",
                            confidence="preliminary",
                            period="second",
                            minute=minute,
                            timestamp=time.time()
                        )

            # --- Abrupt odds-move detection ---
            markets = parsed.get("markets", {})
            odds_delta = self._compute_odds_delta(markets)
            if odds_delta >= self.ODDS_THRESHOLD:
                # Check whether accompanied by score change
                concurrent_score = (new_score != self._last_score) if self._last_score else False
                yield NormalizedEvent(
                    type="odds_spike",
                    source="live_odds",
                    confidence="preliminary",
                    delta=odds_delta,
                    timestamp=time.time()
                )

            # --- Ball position + game state (logging/future extension) ---
            self._ball_pos = info.get("ball_pos", "")
            self._game_state = info.get("state", "")

    def _parse_score(self, score_str: str) -> Tuple[int, int]:
        """'1:0' -> (1, 0)"""
        parts = score_str.split(":")
        return (int(parts[0]), int(parts[1]))

    def _parse_minute(self, minute_str: str) -> Optional[float]:
        """'45' -> 45.0, '90+3' -> 93.0, '' -> None"""
        if not minute_str:
            return None
        if "+" in minute_str:
            base, extra = minute_str.split("+")
            return float(base) + float(extra)
        return float(minute_str)

    def _compute_odds_delta(self, markets: dict) -> float:
        """Compute home-odds change rate in Fulltime Result market."""
        try:
            ft_market = markets.get("1777", {})
            participants = ft_market.get("participants", {})
            for pid, p in participants.items():
                if p.get("short_name") == "Home" or p.get("name", "").endswith("Home"):
                    current = float(p["value_eu"])
                    if self._last_home_odds is not None and self._last_home_odds > 0:
                        delta = abs(current - self._last_home_odds) / self._last_home_odds
                        self._last_home_odds = current
                        return delta
                    self._last_home_odds = current
        except (KeyError, ValueError):
            pass
        return 0.0
```

### Source 2: Goalserve Live Score REST (Authoritative Confirmation, 3-8s)

```python
class GoalserveLiveScoreSource(EventSource):
    """
    REST polling every 3s - 3~8s latency.
    Goal scorer, card details, substitutions, VAR information.

    Goalserve Live Score response format:
    {
      "scores": {
        "category": {
          "matches": {
            "match": [{
              "id": "5035743",
              "localteam": {"goals": "1", "name": "..."},
              "visitorteam": {"goals": "0", "name": "..."},
              "status": "28",
              "timer": "28",
              "events": {
                "event": [{
                  "type": "goal",
                  "team": "localteam",
                  "player": "Lu Pin",
                  "minute": "21",
                  "result": "[1 - 0]"
                }]
              },
              "live_stats": { ... }
            }]
          }
        }
      }
    }
    """

    def __init__(self, api_key: str, match_id: str, poll_interval: float = 3.0):
        self.api_key = api_key
        self.match_id = match_id
        self.poll_interval = poll_interval
        self._last_score = {"home": 0, "away": 0}
        self._last_cards = set()
        self._last_period = None

    async def listen(self) -> AsyncIterator[NormalizedEvent]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            while self.running:
                try:
                    data = await self._poll(client)
                    match = self._find_match(data)
                    if match:
                        async for event in self._diff(match):
                            yield event
                except httpx.HTTPError as e:
                    log.error(f"Live Score poll failed: {e}")
                    self._consecutive_failures += 1
                    if self._consecutive_failures >= 5:
                        yield NormalizedEvent(
                            type="source_failure",
                            source="live_score",
                            confidence="confirmed",
                            timestamp=time.time()
                        )

                await asyncio.sleep(self.poll_interval)

    async def _diff(self, match: dict) -> AsyncIterator[NormalizedEvent]:
        """Detect changes by comparing with previous poll result."""

        # --- Score change (goal confirmed) ---
        home_goals = int(match["localteam"]["goals"] or 0)
        away_goals = int(match["visitorteam"]["goals"] or 0)

        if home_goals > self._last_score["home"]:
            for _ in range(home_goals - self._last_score["home"]):
                yield NormalizedEvent(
                    type="goal_confirmed",
                    source="live_score",
                    confidence="confirmed",
                    score=(home_goals, away_goals),
                    team="localteam",
                    var_cancelled=False,  # Extracted from Live Score events
                    timestamp=time.time()
                )

        if away_goals > self._last_score["away"]:
            for _ in range(away_goals - self._last_score["away"]):
                yield NormalizedEvent(
                    type="goal_confirmed",
                    source="live_score",
                    confidence="confirmed",
                    score=(home_goals, away_goals),
                    team="visitorteam",
                    var_cancelled=False,
                    timestamp=time.time()
                )

        self._last_score = {"home": home_goals, "away": away_goals}

        # --- Red card detection ---
        live_stats = match.get("live_stats", {}).get("value", "")
        home_reds = self._extract_stat(live_stats, "IRedCard", "home")
        away_reds = self._extract_stat(live_stats, "IRedCard", "away")

        current_cards = {("home", home_reds), ("away", away_reds)}
        new_cards = current_cards - self._last_cards
        for team, count in new_cards:
            if count > 0:
                yield NormalizedEvent(
                    type="red_card",
                    source="live_score",
                    confidence="confirmed",
                    team="localteam" if team == "home" else "visitorteam",
                    timestamp=time.time()
                )
        self._last_cards = current_cards

        # --- Period change ---
        status = match.get("status", "")
        if status != self._last_period:
            if status == "HT":
                yield NormalizedEvent(
                    type="period_change",
                    source="live_score",
                    confidence="confirmed",
                    period="Halftime",
                    timestamp=time.time()
                )
            elif status == "Finished":
                yield NormalizedEvent(
                    type="match_finished",
                    source="live_score",
                    confidence="confirmed",
                    timestamp=time.time()
                )
            self._last_period = status
```

### Asynchronous Loop Structure (3 Sources)

```python
async def run_engine(model: LiveFootballQuantModel):
    """Run three coroutines concurrently."""
    await asyncio.gather(
        tick_loop(model),                # Every 1s: μ recompute + P_true output
        live_odds_listener(model),       # WebSocket: <1s event detection
        live_score_poller(model),        # REST every 3s: authoritative confirmation
    )

async def tick_loop(model):
    """Coroutine 1: every-1s tick."""
    while model.engine_phase != FINISHED:
        if model.engine_phase in (FIRST_HALF, SECOND_HALF):
            model.t += 1/60

            # Step 3.2: remaining expected goals
            μ_H, μ_A = compute_remaining_mu(model)

            # Step 3.4: pricing
            P_true, σ_MC = await step_3_4_async(model, μ_H, μ_A)

            # Allow order only if all 3 conditions pass
            order_allowed = (
                not model.cooldown
                and not model.ob_freeze
                and model.event_state == IDLE
            )

            # Send to Phase 4
            emit_to_phase4(P_true, σ_MC, order_allowed, model)

        await asyncio.sleep(1)

async def live_odds_listener(model):
    """Coroutine 2: Live Odds WebSocket (<1s)."""
    async for event in model.live_odds_source.listen():
        if event.type == "goal_detected":
            handle_preliminary_goal(model, event)
        elif event.type == "score_rollback":
            handle_score_rollback(model, event)
        elif event.type == "period_change":
            handle_period_change(model, event)
        elif event.type == "odds_spike":
            handle_odds_spike(model, event)
        elif event.type == "stoppage_entered":
            model.stoppage_mgr.update_from_live_odds(event.minute, event.period)

async def live_score_poller(model):
    """Coroutine 3: Live Score REST (3s polling)."""
    async for event in model.live_score_source.listen():
        if event.type == "goal_confirmed":
            handle_confirmed_goal(model, event)
        elif event.type == "red_card":
            handle_confirmed_red_card(model, event)
        elif event.type == "period_change":
            handle_confirmed_period(model, event)
        elif event.type == "match_finished":
            model.engine_phase = FINISHED
        elif event.type == "source_failure":
            handle_live_score_failure(model)
```

### Event Handlers — Two-Stage Processing (Preliminary -> Confirmed)

#### Preliminary Handler (Live Odds WebSocket, <1s)

```python
def handle_preliminary_goal(model, event: NormalizedEvent):
    """
    Detect score change from Live Odds.
    Still provisional before VAR confirmation.
    """
    # 1. Immediate ob_freeze + state transition
    model.ob_freeze = True
    model.event_state = PRELIMINARY_DETECTED

    # 2. Infer scoring side (from score difference)
    preliminary_score = event.score
    if preliminary_score[0] > model.S[0]:
        scoring_team = "home"
    elif preliminary_score[1] > model.S[1]:
        scoring_team = "away"
    else:
        log.warning("Score changed but neither team increased — ignoring")
        return

    # 3. Provisional ΔS
    preliminary_delta_S = preliminary_score[0] - preliminary_score[1]

    # 4. Precompute μ asynchronously (executor)
    #    for 0ms P_true output when Live Score confirms
    asyncio.create_task(precompute_preliminary_mu(
        model, preliminary_delta_S, scoring_team
    ))

    # 5. Cache provisional payload
    model.preliminary_cache = {
        "score": preliminary_score,
        "delta_S": preliminary_delta_S,
        "scoring_team": scoring_team,
        "timestamp": event.timestamp,
    }

    log.info(f"PRELIMINARY goal: {model.S} -> {preliminary_score} "
             f"(team={scoring_team})")

def handle_score_rollback(model, event: NormalizedEvent):
    """
    Score decreases in Live Odds -> likely VAR cancellation.
    If in preliminary state, rollback immediately.
    """
    if model.event_state == PRELIMINARY_DETECTED:
        log.warning(f"Score rollback: {model.preliminary_cache['score']} -> "
                    f"{event.score} — likely VAR cancellation")
        model.event_state = IDLE
        model.ob_freeze = False
        model.preliminary_cache = {}
    else:
        log.warning(f"Score rollback in state {model.event_state} — logging only")

def handle_odds_spike(model, event: NormalizedEvent):
    """
    Detect only abrupt odds move (without score change).
    Could be goal or red card -> set ob_freeze and wait for Live Score.
    """
    model.ob_freeze = True
    log.warning(f"Odds spike: Δ={event.delta:.3f} — awaiting Live Score confirmation")

def handle_period_change(model, event: NormalizedEvent):
    """Detect period change from Live Odds."""
    if event.period in ("Paused", "Half", "HT"):
        model.engine_phase = HALFTIME
        log.info("HALFTIME detected via Live Odds")
    elif event.period in ("2nd Half", "2nd"):
        model.engine_phase = SECOND_HALF
        log.info("SECOND HALF started via Live Odds")
```

#### Confirmed Handler (Live Score REST, 3-8s)

```python
def handle_confirmed_goal(model, event: NormalizedEvent):
    """
    Goal confirmed by Live Score.
    VAR cancellation, scorer, and assist are now verified.
    """
    # 1. Check VAR cancellation
    if event.var_cancelled:
        model.event_state = IDLE
        model.ob_freeze = False
        model.preliminary_cache = {}
        log.info("Goal VAR cancelled — state rolled back")
        return

    # 2. Commit score
    if event.team == "localteam":
        model.S = (model.S[0] + 1, model.S[1])
    else:
        model.S = (model.S[0], model.S[1] + 1)
    model.delta_S = model.S[0] - model.S[1]

    # 3. Recompute μ — reuse preliminary precompute if available
    if (model.preliminary_cache
        and model.preliminary_cache.get("delta_S") == model.delta_S):
        # Reuse precomputed result -> 0ms
        model.μ_H = model.preliminary_cache["μ_H"]
        model.μ_A = model.preliminary_cache["μ_A"]
        log.info("Using pre-computed μ from preliminary stage")
    else:
        # No cache or mismatched delta_S -> recompute
        model.μ_H, model.μ_A = recompute_mu(model)

    # 4. State transition
    model.cooldown = True
    model.ob_freeze = False
    model.event_state = IDLE
    model.preliminary_cache = {}
    asyncio.create_task(cooldown_timer(model, duration=15))

    log.info(f"CONFIRMED goal: S={model.S}, ΔS={model.delta_S}, "
             f"team={event.team}, scorer={event.scorer_id}")

def handle_confirmed_red_card(model, event: NormalizedEvent):
    """
    Red cards can only be confirmed via Live Score.
    In Live Odds, only indirect detection via odds_spike is possible.
    """
    # 1. Markov state transition
    if event.team == "localteam":
        if model.X == 0: model.X = 1      # 11v11 -> 10v11
        elif model.X == 2: model.X = 3    # 11v10 -> 10v10
    else:  # visitorteam
        if model.X == 0: model.X = 2      # 11v11 -> 11v10
        elif model.X == 1: model.X = 3    # 10v11 -> 10v10

    # 2. Recompute μ — reflect gamma^H, gamma^A updates
    model.μ_H, model.μ_A = recompute_mu(model)

    # 3. State transition
    model.cooldown = True
    model.ob_freeze = False
    model.event_state = IDLE
    asyncio.create_task(cooldown_timer(model, duration=15))

    log.info(f"CONFIRMED red card: X={model.X}, team={event.team}")

def handle_confirmed_period(model, event: NormalizedEvent):
    """Period confirmed by Live Score — cross-check with Live Odds."""
    if event.period == "Halftime" and model.engine_phase != HALFTIME:
        log.warning("Halftime confirmed by Live Score but not detected by Live Odds")
        model.engine_phase = HALFTIME
    elif event.period == "Finished":
        model.engine_phase = FINISHED
```

#### Helper Functions

```python
async def cooldown_timer(model, duration: int = 15):
    """Cooldown timer: release cooldown after duration seconds."""
    await asyncio.sleep(duration)
    model.cooldown = False
    log.info(f"Cooldown expired after {duration}s")

def handle_live_score_failure(model):
    """5 consecutive Live Score polling failures -> stop new orders."""
    model.ob_freeze = True  # safe mode
    log.error("Live Score source failure — freezing all orders")
```

### ob_freeze Release Conditions

```python
def check_ob_freeze_release(model):
    """
    Called every tick. Check ob_freeze release conditions.

    Release if any one condition is met:
    1. Goalserve event detected -> state update complete + entered cooldown
    2. 3 consecutive stable ticks (Live Odds move < threshold)
    3. 10-second timeout (false-positive protection)
    """
    if not model.ob_freeze:
        return

    # Condition 1: explained by event (cooldown takes over)
    if model.cooldown:
        model.ob_freeze = False
        return

    # Condition 2: 3-tick stabilization
    if model._ob_stable_ticks >= 3:
        model.ob_freeze = False
        model._ob_stable_ticks = 0
        log.info("ob_freeze released: 3-tick stabilization")
        return

    # Condition 3: 10-second timeout
    elapsed = time.time() - model._ob_freeze_start
    if elapsed >= 10:
        model.ob_freeze = False
        log.info("ob_freeze released: 10s timeout")
```

### Timeline Comparison

**Original design (REST only):**
```
t=0.0s  Goal occurs (on pitch)
t=1.5s  Kalshi MM quotes start reacting
t=2.0s  Kalshi quote ΔP >= 5c -> ob_freeze (Defense 2)
t=2~6s  blind spot (protected by ob_freeze, but unknown "why")
t=6.0s  Live Score poll detects event (Defense 1)
t=6.0s  state update + 15s cooldown
t=21s   normal operation resumes
```
-> blind spot: **2-6 seconds**

**Full package (3-layer):**
```
t=0.0s  Goal occurs (on pitch)
t=0.5s  bet365 odds jump -> Live Odds WS received
t=0.5s  score "0:0"->"1:0" detected -> PRELIMINARY + ob_freeze (Defense 1)
t=0.5s  μ precompute starts (executor)
t=1.5s  Kalshi MM quote reaction -> cross-check (Defense 2)
t=5.0s  Live Score poll: goal confirmed + scorer + VAR status (Defense 3)
t=5.0s  CONFIRMED -> μ commit (reuse precompute, 0ms) + 15s cooldown
t=20s   normal operation resumes
```
-> blind spot: **~0.5 seconds**

### Output

State vector updated every tick:

$$\text{State}(t) = (t,\; S,\; X,\; \Delta S,\; \text{engine\_phase},\; \text{event\_state},\; \text{cooldown},\; \text{ob\_freeze},\; T)$$

---

## Step 3.2: Remaining Expected Goals Calculation

### Goal

From current time t to match end T,
compute remaining expected goals for home μ_H(t, T)
and away μ_A(t, T).

### Integral Structure

Split remaining interval [t, T] at basis-function boundaries into L subintervals:

$$[t, T] = [t, \tau_1) \cup [\tau_1, \tau_2) \cup \cdots \cup [\tau_{L-1}, T]$$

### Markov-Modulated Integral Formula

Apply team-specific gamma:

$$\boxed{\mu_H(t, T) = \sum_{\ell=1}^{L} \sum_{j=0}^{3} \overline{P}_{X(t),j}^{(\ell)} \cdot \exp\!\left(a_H + b_{i_\ell} + \gamma^H_j + \delta_H(\Delta S)\right) \cdot \Delta\tau_\ell}$$

$$\boxed{\mu_A(t, T) = \sum_{\ell=1}^{L} \sum_{j=0}^{3} \overline{P}_{X(t),j}^{(\ell)} \cdot \exp\!\left(a_A + b_{i_\ell} + \gamma^A_j + \delta_A(\Delta S)\right) \cdot \Delta\tau_\ell}$$

| Term | Meaning |
|----|------|
| $\overline{P}_{X(t),j}^{(\ell)}$ | average probability of being in state j during subinterval ℓ |
| $a_T + b_{i_\ell} + \gamma^T_j + \delta_T(\Delta S)$ | instantaneous scoring intensity for team T ∈ {H, A} |
| $\Delta\tau_\ell$ | subinterval length (minutes) |

> **Fix delta(ΔS):** hold ΔS at the **current** score difference.
> Future ΔS changes due to goals are handled by Monte Carlo in Step 3.4.

### Matrix Exponential Lookup

```python
def get_transition_prob(model, dt_min: float) -> np.ndarray:
    """
    Lookup transition probability from P_grid or P_fine_grid.
    Use fine grid near match end.
    """
    if dt_min <= 5 and hasattr(model, 'P_fine_grid'):
        # Fine grid: 10-second increments (near match end)
        dt_10sec = int(round(dt_min * 6))
        dt_10sec = max(0, min(30, dt_10sec))
        return model.P_fine_grid[dt_10sec]
    else:
        # Standard grid: 1-minute increments
        dt_round = max(0, min(100, round(dt_min)))
        return model.P_grid[dt_round]
```

### Preliminary Precomputation

```python
async def precompute_preliminary_mu(model, preliminary_delta_S, scoring_team):
    """
    Called immediately when Live Odds detects goal.
    Precompute μ before Live Score confirmation -> 0ms at confirmation.
    """
    loop = asyncio.get_event_loop()

    # delta index
    di = max(0, min(4, preliminary_delta_S + 2))

    # Compute μ_H, μ_A (analytic or MC, in executor)
    if model.X == 0 and preliminary_delta_S == 0 and not model.DELTA_SIGNIFICANT:
        # Analytic - immediate
        μ_H, μ_A = analytical_remaining_mu(model, preliminary_delta_S)
    else:
        # MC - executor
        final_scores = await loop.run_in_executor(
            mc_executor,
            mc_simulate_remaining,
            model.t, model.T, model.S[0], model.S[1],
            model.X, preliminary_delta_S,
            model.a_H, model.a_A, model.b,
            model.gamma_H, model.gamma_A,
            model.delta_H, model.delta_A,
            model.Q_diag, model.Q_off_normalized,
            model.basis_bounds, N_MC,
            int(time.time() * 1000) % (2**31)
        )
        μ_H = np.mean(final_scores[:, 0]) - model.S[0]
        μ_A = np.mean(final_scores[:, 1]) - model.S[1]

    # Store in cache
    model.preliminary_cache["μ_H"] = μ_H
    model.preliminary_cache["μ_A"] = μ_A

    log.info(f"Preliminary μ computed: μ_H={μ_H:.3f}, μ_A={μ_A:.3f}")
```

### Output

Every tick: μ_H(t, T), μ_A(t, T).

---

## Step 3.3: Discrete Shock Handling (Discrete Event Handler)

### Event-Source Role Matrix

| Event | Primary Detection (Live Odds WS, <1s) | Confirmation (Live Score REST, 3-8s) |
|--------|------------------------------|------------------------------|
| **Goal** | score field change -> PRELIMINARY | goal scorer + VAR status -> CONFIRMED |
| **Red card** | odds jump -> ob_freeze (type unknown) | redcards diff -> CONFIRMED |
| **Halftime** | period "Paused" -> engine_phase transition | period "Half" -> cross-check |
| **Stoppage time** | minute > 45 / > 90 -> T rolling | minute field -> cross-check |
| **VAR review** | odds oscillation (up then down) | var_cancelled field |
| **VAR cancellation** | score decrease -> score_rollback | var_cancelled=True |
| **Substitution** | not detectable | substitutions diff (logging) |

### Event 1: Goal — Two-Stage Processing

**Stage 1 — Preliminary (Live Odds, <1s):**
- detect score change -> `PRELIMINARY_DETECTED`
- ob_freeze = True
- μ precompute using provisional ΔS
- block Phase 4 orders

**Stage 2 — Confirmed (Live Score, 3-8s):**
- check var_cancelled
- **if not cancelled:** commit S -> apply δ_H(ΔS), δ_A(ΔS) -> commit μ -> cooldown 15s
- **if cancelled:** rollback state -> release ob_freeze

### Event 2: Red Card — Confirmed Only via Live Score

Live Odds can only detect "something happened" via abrupt odds movement.
Abrupt odds move without score change -> possible red card or VAR review.

| Transition | Trigger | gamma^H change | gamma^A change |
|------|--------|----------|----------|
| 0 -> 1 | home dismissal | 0 -> gamma^H₁ < 0 (home down) | 0 -> gamma^A₁ > 0 (away up) |
| 0 -> 2 | away dismissal | 0 -> gamma^H₂ > 0 (home up) | 0 -> gamma^A₂ < 0 (away down) |
| 1 -> 3 | additional away dismissal | gamma^H₁ -> gamma^H₁+gamma^H₂ | gamma^A₁ -> gamma^A₁+gamma^A₂ |
| 2 -> 3 | additional home dismissal | gamma^H₂ -> gamma^H₁+gamma^H₂ | gamma^A₂ -> gamma^A₁+gamma^A₂ |

When red card is confirmed, μ_H and μ_A move in **opposite directions**:
home dismissal -> μ_H down + μ_A up.

### Event 3: Halftime

| Action | Primary (Live Odds) | Confirmed (Live Score) |
|------|-----------------|-------------------|
| first-half end | period="Paused" -> HALFTIME | period="HT" -> cross-check |
| second-half start | period="2nd Half" -> SECOND_HALF | status change -> cross-check |

### Event 4: Stoppage Time

Both Live Odds and Live Score provide minute values, enabling cross-validation.
Handled in detail in Step 3.5.

### Cooldown

| Item | Value |
|------|---|
| detection -> confirmation delay | <1s (Live Odds) + 3-8s (Live Score) |
| cooldown duration | **15s** (from confirmation time) |
| P_true calculation during cooldown | continues (monitoring) |
| orders during cooldown | **blocked** |

### Output

Re-adjusted state vector (S, X, ΔS, μ_H, μ_A, T) and event_state/cooldown/ob_freeze status.

---

## Step 3.4: Pricing — True Probability Estimation

### Goal

Using remaining expected goals μ_H and μ_A,
estimate true probabilities (P_true)
that can be compared against Kalshi order books.

### Independence Assumption Analysis

With delta(ΔS), when one team scores, both teams' lambda change simultaneously,
so home/away scoring independence breaks.

Even starting from X = 0, ΔS = 0, once first goal occurs, ΔS = ±1,
and if delta(±1) ≠ 0, subsequent intensities are coupled.
Therefore analytic Poisson/Skellam is only a **first-order approximation**.

### Hybrid Pricing

| Condition | Method | Accuracy |
|------|------|--------|
| X=0, ΔS=0, delta not significant | analytic Poisson/Skellam | **exact** |
| X=0, ΔS=0, delta significant | analytic (first-order approx) | **approximate** — ignores delta feedback |
| X≠0 or ΔS≠0 | Monte Carlo simulation | **exact** (with sufficient N) |

> **Practical guide:** if delta is small (|delta| < 0.1), analytic approximation error may be below MC standard error.
> If delta is larger (|delta| >= 0.15), MC is safer even at ΔS = 0.
> With Numba JIT + executor, MC overhead is ~0.5ms/match, so always-MC is also practical.

### Logic A: Analytic Pricing (X=0, ΔS=0)

Let $G = S_H + S_A$ be current total goals:

**Over/Under:**

$$P_{true}(\text{Over } N\text{.5}) = \begin{cases} 1 & \text{if } G > N \\ 1 - \sum_{k=0}^{N-G} \frac{\mu_{total}^k \cdot e^{-\mu_{total}}}{k!} & \text{if } G \leq N \end{cases}$$

**Match Odds (Skellam):**

$$P_{true}(\text{Home Win}) = \sum_{D=1}^{\infty} e^{-(\mu_H + \mu_A)} \left(\frac{\mu_H}{\mu_A}\right)^{D/2} I_{|D|}(2\sqrt{\mu_H \mu_A})$$

Analytic mode executes immediately on main thread (~0.1ms).

### Logic B: Monte Carlo Pricing (X≠0 or ΔS≠0)

#### Numba JIT-compiled MC Core

```python
@njit(cache=True)
def mc_simulate_remaining(
    t_now, T_end, S_H, S_A, state, score_diff,
    a_H, a_A,
    b,                  # shape (6,)
    gamma_H, gamma_A,   # shape (4,) each
    delta_H, delta_A,   # shape (5,) each
    Q_diag,             # shape (4,)
    Q_off,              # shape (4,4) — normalized transition probabilities
    basis_bounds,       # shape (7,)
    N, seed
):
    """
    Returns: final_scores — shape (N, 2)
    Uses team-specific gamma + normalized Q_off.
    """
    np.random.seed(seed)
    results = np.empty((N, 2), dtype=np.int32)

    for sim in range(N):
        s = t_now
        sh, sa = S_H, S_A
        st = state
        sd = score_diff

        while s < T_end:
            # current basis index
            bi = 0
            for k in range(6):
                if s >= basis_bounds[k] and s < basis_bounds[k + 1]:
                    bi = k
                    break

            # delta index: ΔS -> {0:<=-2, 1:-1, 2:0, 3:+1, 4:>=+2}
            di = max(0, min(4, sd + 2))

            # use team-specific gamma
            lam_H = np.exp(a_H + b[bi] + gamma_H[st] + delta_H[di])
            lam_A = np.exp(a_A + b[bi] + gamma_A[st] + delta_A[di])
            lam_red = -Q_diag[st]
            lam_total = lam_H + lam_A + lam_red

            if lam_total <= 0:
                break

            # waiting time to next event
            dt = -np.log(np.random.random()) / lam_total
            s_next = s + dt

            # basis boundary or match-end check
            next_bound = T_end
            for k in range(7):
                if basis_bounds[k] > s:
                    next_bound = min(next_bound, basis_bounds[k])
                    break

            if s_next >= next_bound:
                s = next_bound
                continue

            s = s_next

            # sample event
            u = np.random.random() * lam_total
            if u < lam_H:
                sh += 1
                sd += 1
            elif u < lam_H + lam_A:
                sa += 1
                sd -= 1
            else:
                # use normalized Q_off
                cum = 0.0
                r = np.random.random()
                for j in range(4):
                    if j == st:
                        continue
                    cum += Q_off[st, j]
                    if r < cum:
                        st = j
                        break

        results[sim, 0] = sh
        results[sim, 1] = sa

    return results
```

**Performance:**

| Implementation | Latency | 10 Matches in Parallel |
|------|----------|-----------|
| pure Python | ~50ms | ~500ms ❌ |
| **Numba @njit** | **~0.5ms** | **~5ms ✅** |

#### Executor Decoupling

```python
mc_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mc")

async def step_3_4_async(model, μ_H, μ_A):
    """Non-blocking async pricing that does not block main event loop."""

    if model.X == 0 and model.delta_S == 0 and not model.DELTA_SIGNIFICANT:
        P_true = analytical_pricing(μ_H, μ_A, model.S)
        σ_MC = 0.0
        return P_true, σ_MC

    else:
        loop = asyncio.get_event_loop()
        model._mc_version += 1
        my_version = model._mc_version

        seed = hash((model.match_id, model.t, model.S[0],
                      model.S[1], model.X)) % (2**31)

        final_scores = await loop.run_in_executor(
            mc_executor,
            mc_simulate_remaining,
            model.t, model.T, model.S[0], model.S[1],
            model.X, model.delta_S,
            model.a_H, model.a_A, model.b,
            model.gamma_H, model.gamma_A,
            model.delta_H, model.delta_A,
            model.Q_diag, model.Q_off_normalized,
            model.basis_bounds, N_MC, seed
        )

        # stale check
        if my_version != model._mc_version:
            return None, None
        if model.event_state == PRELIMINARY_DETECTED:
            return None, None

        P_true = aggregate_markets(final_scores, model.S)
        σ_MC = compute_mc_stderr(P_true, N_MC)
        return P_true, σ_MC
```

> **Deterministic MC seeds:** use seeds based on `hash(match_id, t, S_H, S_A, X)`
> to guarantee reproducibility in identical states.
> This enables exact reproduction for debugging and backtesting.

### Market Probability Estimation (Aggregate MC Results)

A single MC batch yields probabilities for **all markets simultaneously**:

```python
def aggregate_markets(final_scores: np.ndarray, current_S: Tuple[int,int]) -> dict:
    N = len(final_scores)
    sh = final_scores[:, 0]
    sa = final_scores[:, 1]
    total = sh + sa

    return {
        # Over/Under
        "over_15": np.mean(total > 1),
        "over_25": np.mean(total > 2),
        "over_35": np.mean(total > 3),

        # Match Odds
        "home_win": np.mean(sh > sa),
        "draw": np.mean(sh == sa),
        "away_win": np.mean(sh < sa),

        # Both Teams to Score
        "btts_yes": np.mean((sh > 0) & (sa > 0)),

        # Correct Score (top-probability scores only)
        # ...
    }
```

### Output

Every second:
- P_true(t): true probability by active market
- σ_MC(t): Monte Carlo standard error (analytic mode: 0)
- pricing_mode: Analytical / Monte Carlo

---

## Step 3.5: Real-Time Stoppage-Time Handling

### Goal

Adjust $T_{exp}$ from Phase 2 in real time as match evolves.

### Dual-Source Cross-Validation

Because both Live Odds (minute, <1s) and Live Score (timer, 3-8s) provide minute fields,
cross-validation improves data reliability.

```python
class StoppageTimeManager:
    def __init__(self, T_exp: float, rolling_horizon: float = 1.5):
        self.T_exp = T_exp
        self.rolling_horizon = rolling_horizon
        self.first_half_stoppage = False
        self.second_half_stoppage = False
        self._lo_minute = None   # minute from Live Odds
        self._ls_minute = None   # minute from Live Score

    def update_from_live_odds(self, minute: float, period: str) -> float:
        """Live Odds WebSocket — faster updates (<1s)."""
        self._lo_minute = minute
        return self._compute_T(minute, period)

    def update_from_live_score(self, minute: float, period: str) -> float:
        """Live Score REST — authoritative updates (3-8s)."""
        self._ls_minute = minute

        # Cross-validation: warn if minute differs by 2+ mins across sources
        if self._lo_minute and abs(self._lo_minute - minute) > 2:
            log.warning(
                f"Minute mismatch: LiveOdds={self._lo_minute}, "
                f"LiveScore={minute}"
            )
        return self._compute_T(minute, period)

    def _compute_T(self, minute: float, period: str) -> float:
        # Phase B: first-half stoppage
        if period in ("1st Half", "1st") and minute > 45:
            if not self.first_half_stoppage:
                self.first_half_stoppage = True
            # Keep T_game unchanged; only adjust basis boundary
            # (first-half end is finalized by halftime event)
            return self.T_exp

        # Phase C: second-half stoppage
        if period in ("2nd Half", "2nd") and minute > 90:
            if not self.second_half_stoppage:
                self.second_half_stoppage = True
            # Rolling update for T_game
            return minute + self.rolling_horizon

        # Phase A: regular time
        return self.T_exp
```

> **Phase B vs Phase C distinction:**
> In Phase B (first-half stoppage), do not modify T_game.
> First-half end is determined by halftime entry event,
> so T_game rolling applies only in second-half stoppage (Phase C).

### Stoppage-Time Uncertainty Modeling (Optional Extension)

In Monte Carlo, sample path-level T from a stoppage-time distribution (Log-Normal or Gamma)
to naturally include uncertainty.

### Output

Real-time updated T.

---

## Phase 3 -> Phase 4 Handoff

| Item | Value | Update Frequency |
|------|---|-------------|
| P_true(t) | true probability by market | every 1s |
| σ_MC(t) | MC standard error | every 1s (analytic: 0) |
| **order_allowed** | **NOT cooldown AND NOT ob_freeze AND event_state == IDLE** | every 1s + on events |
| pricing_mode | Analytical / Monte Carlo | switches on events |
| μ_H, μ_A | remaining expected goals | every 1s (for logging) |
| engine_phase | current match phase | on period change |
| **event_state** | **IDLE / PRELIMINARY / CONFIRMED** | on events |
| **P_bet365(t)** | **bet365 in-play implied probability** | **every push (<1s)** |
| **ball_pos, game_state** | **ball position + game state** | **every push (logging/future extension)** |

---

## Phase 3 Pipeline Summary

```
[Kickoff - engine_phase: FIRST_HALF]
              |
              v
+-----------------------------------------------------------------+
|  Step 3.1: State Machine + 3-Layer Detection                     |
|                                                                 |
|  +--------------------+  +------------------+  +-------------+ |
|  | Live Odds WS       |  | Kalshi WS        |  | Live Score  | |
|  | (<1s, PUSH)        |  | (1-2s)           |  | (3-8s,REST) | |
|  |                    |  |                  |  |             | |
|  | score change:      |  | quote ingest:    |  | goal confirm:|
|  | -> PRELIMINARY     |  | -> cross-check   |  | -> CONFIRMED |
|  | -> ob_freeze       |  | -> send to Ph4   |  | -> VAR check |
|  |                    |  |                  |  |             | |
|  | odds jump:         |  |                  |  | red card:   | |
|  | -> ob_freeze       |  |                  |  | -> CONFIRMED|
|  |                    |  |                  |  |             | |
|  | period/minute:     |  |                  |  | period:     | |
|  | -> halftime/stoppage| |                  |  | -> cross-check|
|  |                    |  |                  |  |             | |
|  | bet365 odds:       |  |                  |  |             | |
|  | -> P_bet365 (for Ph4)| |                 |  |             | |
|  +--------+-----------+  +------------------+  +------+------+
|           |                                           |        |
|           +--------------+----------------------------+        |
|                          v                                     |
|           Event State Machine                                  |
|           IDLE -> PRELIMINARY -> CONFIRMED -> COOLDOWN -> IDLE |
|                          \ FALSE_ALARM -> IDLE                  |
|                          \ VAR_CANCELLED -> IDLE                |
|                                                                 |
|           order_allowed = NOT cooldown                          |
|                          AND NOT ob_freeze                      |
|                          AND event_state == IDLE                |
+------------------+----------------------------------------------+
                   |
        +----------+----------+
        | (every 1s tick)    | (on event detection)
        v                     v
+------------------+  +----------------------------------------+
|  Step 3.2:       |  |  Step 3.3: Discrete Shock Handling      |
|  Remaining μ     |  |                                        |
|                  |  |  • Goal: preliminary -> confirmed (VAR)|
|  • Piecewise int |  |  • Red card: X transition, gamma^H/A   |
|  • P_grid lookup |  |  • Halftime: freeze/resume             |
|  • Team gamma    |  |  • Stoppage: T rolling (dual source)   |
|  • delta(ΔS) adj |  |  • μ precompute (preliminary)          |
|  Output: μ_H,μ_A |  +--------+-------------------------------+
+--------+---------+           |
         |                     |
         +----------+----------+
                    v
+-----------------------------------------------------------------+
|  Step 3.4: Pricing (True Probability)                            |
|                                                                 |
|  +-----------------------+  +------------------------------+    |
|  | X=0, ΔS=0, delta not  |  | Otherwise                    |    |
|  | significant?          |  | -> Numba MC (ThreadPool)     |    |
|  | -> analytic (0.1ms)   |  |    N=50000, ~0.5ms/match     |    |
|  |    σ_MC = 0           |  |    deterministic seed         |    |
|  +-----------+-----------+  |    stale + preliminary check  |    |
|              |              +--------------+---------------+    |
|              +----------+-------------------+                    |
|                         v                                        |
|  Output: P_true(t), σ_MC(t), pricing_mode                        |
+------------------+----------------------------------------------+
                   |
                   v
+-----------------------------------------------------------------+
|  Step 3.5: Stoppage-Time Handling (Dual-Source Cross-Validation) |
|  • Live Odds minute (<1s) + Live Score timer (3-8s)             |
|  • Phase B (1H): keep T_game, confirm via halftime              |
|  • Phase C (2H): T = minute + rolling 1.5 min                  |
+------------------+----------------------------------------------+
                   |
                   v
         [Phase 4: Arbitrage & Execution]
         (order_allowed + P_true + σ_MC + P_bet365)
```
