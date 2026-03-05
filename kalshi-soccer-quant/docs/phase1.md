# Phase 1: Offline Calibration — Goalserve Full Package

## Overview

This is the stage where all MMPP (Markov-Modulated Poisson Process) parameters are learned from historical data.
If this stage is weak, every downstream live-trading calculation becomes Garbage In, Garbage Out.

Using tens of thousands of matches accumulated in the Goalserve full package,
we break the process of extracting model parameters into five linear steps.

### Unified Data Source

Unify all of Phases 1-4 under a **single Goalserve source**.
This fundamentally removes schema mismatches and ID-mapping errors.

| Goalserve Package | Role in Phase 1 | Core Data |
|------------------|-----------------|-----------|
| **Fixtures/Results** | Interval segmentation + event timeline | goals (minute+VAR), red cards (minute), stoppage time, halftime score, lineups |
| **Live Game Stats** | Team/player stats + xG | per-half team stats, detailed player stats (rating, passes, shots, etc.), xG |
| **Pregame Odds** | Odds features + market baseline | 20+ bookmaker odds (open/close), 50+ markets |

### Scoring Intensity Function (Final Form)

Home and away teams use **separate gamma and delta parameters**.

$$\lambda_H(t \mid X, \Delta S) = \exp\!\left(a_H + b_{i(t)} + \gamma^H_{X(t)} + \delta_H(\Delta S(t))\right)$$

$$\lambda_A(t \mid X, \Delta S) = \exp\!\left(a_A + b_{i(t)} + \gamma^A_{X(t)} + \delta_A(\Delta S(t))\right)$$

| Symbol | Meaning | Estimated in Step |
|------|------|----------|
| $a_H, a_A$ | Match-level baseline scoring intensity (team strength) | Step 1.3 init -> Step 1.4 correction |
| $b_{i(t)}$ | Time-interval scoring profile | Step 1.4 |
| $\gamma^H_{X(t)}$ | Red-card state -> **home-team** scoring penalty | Step 1.4 |
| $\gamma^A_{X(t)}$ | Red-card state -> **away-team** scoring penalty | Step 1.4 |
| $\delta_H(\Delta S)$ | Score-difference -> home tactical effect | Step 1.4 |
| $\delta_A(\Delta S)$ | Score-difference -> away tactical effect | Step 1.4 |

---

## Input Data

From the three APIs in the Goalserve full package:

**1. Fixtures/Results — historical 5+ seasons, 500+ leagues:**

```
GET /getfeed/{api_key}/soccerfixtures/league/{league_id}?json=1
```

- Match-level event timeline: `summary.{team}.goals`, `summary.{team}.redcards`, `summary.{team}.yellowcards`
- Lineups: `teams.{team}.player[]` (formation_pos, pos, id)
- Substitutions: `substitutions.{team}.substitution[]`
- Match metadata: `matchinfo.time.addedTime_period1/2`, `{team}.ht_score`, `{team}.ft_score`
- Status: `status` (Full-time, Postponed, Cancelled, etc.)

**2. Live Game Stats — detailed historical match stats (100+ leagues):**

```
GET /getfeed/{api_key}/soccerstats/match/{match_id}?json=1
```

- Team stats: `stats.{team}` — shots, passes, possession, corners, fouls, saves (per-half)
- Player stats: `player_stats.{team}.player[]` — rating, goals, assists, shots, passes, tackles, interceptions, minutes_played, etc.
- xG: Expected Goals (included in the Live Game Stats package)

**3. Pregame Odds — historical odds (500+ leagues, 20+ bookmakers):**

```
GET /getfeed/{api_key}/soccernew/{league_id}?json=1
```

- Bookmaker odds: `bookmaker[].odd[]` (name, value)
- 50+ markets: Match Winner, Over/Under, Asian Handicap, etc.
- Historical odds: includes open/close lines

---

## Step 1.1: Time-Series Event Segmentation and Intervalization (Data Engineering)

### Goal

Convert point events in historical matches into **continuous intervals where lambda is constant**.

Because intensity function lambda depends on $(X(t), \Delta S(t))$,
the interval must be split whenever either variable changes.

### Goalserve Data Mapping

**Goal events — `summary.{team}.goals.player[]`:**

```json
{
  "id": "119",
  "minute": "23",
  "extra_min": "",
  "name": "Lionel Messi",
  "penalty": "True",
  "owngoal": "False",
  "var_cancelled": "False"
}
```

| Field | Usage |
|------|------|
| `minute` + `extra_min` | Goal timestamp (stoppage-time goal: `minute`=90, `extra_min`=3 -> 93rd minute) |
| `{team}` key (localteam/visitorteam) | Identify scoring team -> branch ln lambda_H vs ln lambda_A in NLL |
| `owngoal` | If True, flip scoring team (own goal increments opponent score) |
| **`var_cancelled`** | **If True, fully exclude from interval splitting** |
| `penalty` | Logging/analysis purpose (whether penalty kick) |

> **VAR-cancelled goal handling — an important addition not in the original design:**
> Since goals with `var_cancelled = "True"` did not actually change ΔS,
> they must be completely excluded from interval splitting.
> Ignoring this contaminates ΔS and introduces systematic bias in delta estimation.

**Red card events — `summary.{team}.redcards.player[]`:**

```json
{
  "id": "...",
  "minute": "35",
  "extra_min": "",
  "name": "Player Name"
}
```

| Field | Usage |
|------|------|
| `minute` + `extra_min` | Dismissal timestamp |
| `{team}` key | Team dismissed -> determines X(t) transition direction |

> **Second-yellow dismissal check:**
> During trial period, verify whether `summary.redcards` includes second-yellow dismissals.
> If not, complement via cross-check with `player_stats.{team}.player[].redcards`.

**Stoppage time — `matchinfo.time`:**

```json
{
  "addedTime_period1": "7",
  "addedTime_period2": "8"
}
```

Actual match end time per match:

$$T_m = 90 + \alpha_1 + \alpha_2$$

Since first/second-half stoppage time is provided directly,
ambiguous methods such as "estimating the final play timestamp" are unnecessary.

**Halftime — `{team}.ht_score`:**

```json
"localteam": { "ht_score": "2", "ft_score": "3" },
"visitorteam": { "ht_score": "0", "ft_score": "3" }
```

Because the exact halftime score is known,
ΔS at the halftime boundary can be determined unambiguously.

### Interval Boundary (Split Point) Rules

| Event | Split? | Reason |
|--------|----------|------|
| Goal (`var_cancelled=False`) | Yes | ΔS changes -> delta changes |
| Goal (`var_cancelled=True`) | **No** | ΔS unchanged — cancelled goal |
| Red card | Yes | X(t) changes -> gamma changes |
| Halftime start | Yes | Excluded from integration |
| Halftime end | Yes | Resume integration |
| Match end | Yes | Close interval |
| Yellow card, substitution | No | Not included in current state variables |

### Halftime Handling

During halftime, lambda(t) = 0. If this segment is included in integration,
"a long period with no events" distorts estimation of time profile b_i.

**Effective play-time transform:**

$$t_{eff} = \begin{cases} t & \text{if } t < 45 + \alpha_1 \\ t - \delta_{HT} & \text{if } t \geq 45 + \alpha_1 + \delta_{HT} \end{cases}$$

$\alpha_1$: first-half stoppage time (`addedTime_period1`),
$\delta_{HT}$: halftime break length (about 15 min).

Mark halftime as a separate flag and fully exclude it from NLL integration.

### Dual Role of Goals

due to the introduction of delta(ΔS), goals have two simultaneous roles:

1. **Interval boundary:** a new interval starts right after the goal; new interval delta uses post-goal ΔS.
2. **Point event:** contributes to the NLL term Σ ln lambda(t_i). Here, delta must use **pre-goal** ΔS.

> **Causality caution:** when home scores from 0-0,
> applying delta(+1) to that goal's lambda contribution reflects "scoring power while already ahead,"
> which reverses causality. For the goal-time NLL contribution, always use **pre-goal** ΔS.

### Own Goal Handling

For `owngoal = "True"`, the recorded team and actual scoring team are opposite:

```python
def resolve_scoring_team(goal_event, recorded_team):
    """Flip scoring team for own goals."""
    if goal_event["owngoal"] == "True":
        return "visitorteam" if recorded_team == "localteam" else "localteam"
    return recorded_team
```

Because own goals are exogenous stochastic events rather than intentional attacking outcomes,
it is ambiguous which team's ln lambda should receive point-event credit in NLL.

**Policy:**
- Exclude own goals from point-event term (Σ ln lambda).
- Keep them in interval integration term (Σ mu_k), since ΔS still changes in reality.
- In short, own goals are treated as "events that change score but prove neither team's scoring intensity."

> **Rationale:** lambda_H models "intensity of intentional home scoring."
> An away defender's own goal is not part of this intensity.
> Including own goals in ln lambda_H biases lambda_H upward and inflates a_H.

### Data Transformation Example

**Goalserve raw data (2022 World Cup Final):**

```json
"matchinfo": {
  "time": { "addedTime_period1": "7", "addedTime_period2": "8" }
},
"localteam": { "name": "Argentina", "ht_score": "2", "ft_score": "3" },
"visitorteam": { "name": "France", "ht_score": "0", "ft_score": "3" },
"summary": {
  "localteam": {
    "goals": {
      "player": [
        {"minute": "23", "name": "Messi", "penalty": "True", "var_cancelled": "False"},
        {"minute": "36", "name": "Di María", "penalty": "False", "var_cancelled": "False"},
        {"minute": "108", "name": "Messi", "penalty": "False", "var_cancelled": "False"}
      ]
    },
    "redcards": null
  },
  "visitorteam": {
    "goals": {
      "player": [
        {"minute": "80", "name": "Mbappé", "penalty": "True", "var_cancelled": "False"},
        {"minute": "81", "name": "Mbappé", "penalty": "False", "var_cancelled": "False"},
        {"minute": "118", "name": "Mbappé", "penalty": "True", "var_cancelled": "False"}
      ]
    },
    "redcards": null
  }
}
```

**Transformed output (T_m = 90 + 7 + 8 = 105, with extra time extending to 120+):**

| Interval | Time Range | X | ΔS | delta | Point Event | Scoring Team |
|------|----------|---|-----|---|----------|---------|
| 1 | [0, 23) | 0 | 0 | delta(0)=0 | — | — |
| 2 | [23, 36) | 0 | +1 | delta(+1) | t=23, delta_before=delta(0) | **Home** |
| 3 | [36, 45+7) | 0 | +2 | delta(+2) | t=36, delta_before=delta(+1) | **Home** |
| — | HT | — | — | — | **Halftime: excluded from integration** | — |
| 4 | [HT_end, 80) | 0 | +2 | delta(+2) | — | — |
| 5 | [80, 81) | 0 | +1 | delta(+1) | t=80, delta_before=delta(+2) | **Away** |
| 6 | [81, 90+8) | 0 | 0 | delta(0)=0 | t=81, delta_before=delta(+1) | **Away** |
| ... | Extra time continues | | | | | |

### Interval Record Schema

```python
@dataclass
class IntervalRecord:
    match_id: str           # Goalserve match ID (unified across all phases)
    t_start: float          # Interval start (effective play time)
    t_end: float            # Interval end
    state_X: int            # Markov state {0,1,2,3}
    delta_S: int            # Score difference (home - away)
    home_goal_times: list   # Home goal timestamps in this interval
    away_goal_times: list   # Away goal timestamps in this interval
    goal_delta_before: list # Pre-goal ΔS for each goal
    T_m: float              # Actual match end time
    is_halftime: bool       # Whether this interval is halftime
    alpha_1: float          # First-half stoppage time (addedTime_period1)
    alpha_2: float          # Second-half stoppage time (addedTime_period2)
```

### ETL Pipeline

```python
def build_intervals_from_goalserve(match_data: dict) -> List[IntervalRecord]:
    """Goalserve Fixtures/Results -> interval record transformation."""

    # 1. Extract stoppage time
    alpha_1 = float(match_data["matchinfo"]["time"]["addedTime_period1"] or 0)
    alpha_2 = float(match_data["matchinfo"]["time"]["addedTime_period2"] or 0)
    T_m = 90 + alpha_1 + alpha_2

    # 2. Collect events + VAR-cancelled filtering
    events = []

    for team_key in ["localteam", "visitorteam"]:
        goals = match_data["summary"][team_key].get("goals", {})
        if goals:
            for g in ensure_list(goals.get("player", [])):
                if g.get("var_cancelled") == "True":
                    continue  # Exclude VAR-cancelled goals

                scoring_team = resolve_scoring_team(g, team_key)
                minute = parse_minute(g["minute"], g.get("extra_min", ""))
                events.append(Event("goal", minute, scoring_team, g))

        redcards = match_data["summary"][team_key].get("redcards", {})
        if redcards:
            for r in ensure_list(redcards.get("player", [])):
                minute = parse_minute(r["minute"], r.get("extra_min", ""))
                events.append(Event("red_card", minute, team_key, r))

    # Add halftime boundaries
    events.append(Event("halftime_start", 45 + alpha_1, None, None))
    events.append(Event("halftime_end", 45 + alpha_1 + 15, None, None))  # ~15 min break
    events.append(Event("match_end", T_m, None, None))

    # 3. Sort by time and split into intervals
    events.sort(key=lambda e: e.minute)
    intervals = split_into_intervals(events, T_m, alpha_1, alpha_2)

    return intervals
```

### Output

Tens of thousands of matches are transformed into hundreds of thousands of interval records.
Every record is tagged with Goalserve `match_id`, enabling lookup by the same ID in later phases.

---

## Step 1.2: Estimating Markov Chain Generator Matrix Q (Empirical + Shrinkage)

### Goal

Estimate red-card transition rates from historical data and construct a 4x4 generator matrix Q.

### State Space

| State | Meaning |
|------|------|
| 0 | 11v11 (normal) |
| 1 | 10v11 (home sent off) |
| 2 | 11v10 (away sent off) |
| 3 | 10v10 (both teams sent off) |

### Goalserve Data Mapping

**Red-card timeline — Fixtures/Results `summary.{team}.redcards`:**

Since each red card has exact minute and team (localteam/visitorteam),
the match-level Markov state path can be fully reconstructed:

```python
def reconstruct_markov_path(match_data: dict) -> List[Tuple[float, int]]:
    """Reconstruct match Markov state path: [(time, state), ...]."""
    path = [(0, 0)]  # Kickoff: state 0 (11v11)
    current_state = 0

    red_events = []
    for team_key in ["localteam", "visitorteam"]:
        redcards = match_data["summary"][team_key].get("redcards", {})
        if redcards:
            for r in ensure_list(redcards.get("player", [])):
                minute = parse_minute(r["minute"], r.get("extra_min", ""))
                red_events.append((minute, team_key))

    red_events.sort(key=lambda x: x[0])

    for minute, team in red_events:
        if team == "localteam":
            if current_state == 0: current_state = 1      # 11v11 -> 10v11
            elif current_state == 2: current_state = 3    # 11v10 -> 10v10
        else:
            if current_state == 0: current_state = 2      # 11v11 -> 11v10
            elif current_state == 1: current_state = 3    # 10v11 -> 10v10
        path.append((minute, current_state))

    return path
```

**Cross-check — Live Game Stats `player_stats.{team}.player[].redcards`:**

Compare `summary.redcards` from Fixtures/Results against player-level redcard fields from Live Game Stats
to detect missing data (especially second-yellow dismissals).

### Baseline Estimator

$$q_{ij} = \frac{N_{ij}}{\sum_m \int_0^{T_m} \mathbb{1}_{\{X_m(t) = i\}}\, dt}$$

- Numerator $N_{ij}$: number of observed i -> j transitions across all data
- Denominator: total **effective play time** spent in state i across all matches
  - Halftime excluded
  - Match-specific $T_m = 90 + \alpha_1 + \alpha_2$ (from Goalserve `addedTime`)
- Diagonal terms: $q_{ii} = -\sum_{j \neq i} q_{ij}$

### Sparse-State Handling (State 3: 10v10)

Additivity assumption:

$$q_{1 \to 3} \approx q_{0 \to 2}, \quad q_{2 \to 3} \approx q_{0 \to 1}$$

Scoring penalties are also additive by team:

$$\gamma^H_3 = \gamma^H_1 + \gamma^H_2, \quad \gamma^A_3 = \gamma^A_1 + \gamma^A_2$$

### League-Stratified Estimation

Because Goalserve Fixtures/Results covers 500+ leagues, there is enough data to estimate league-specific Q.

- **Option A — independent Q per league:** independent estimates for Kalshi-tradable leagues (EPL, La Liga, Bundesliga, Serie A, Ligue 1)
- **Option B — hierarchical Bayesian:** use all leagues as a prior pool and update each league posterior; better for low-data leagues

### Q_off Normalization (for MC simulation)

In Phase 3 Step 3.4 Monte Carlo, to decide "which state transition occurs when a dismissal event happens,"
the off-diagonal entries of Q must be normalized into **transition probabilities**:

```python
Q_off_normalized = np.zeros((4, 4))
for i in range(4):
    total_off_diag = -Q[i, i]  # = Σ_{j≠i} Q[i,j]
    if total_off_diag > 0:
        for j in range(4):
            if i != j:
                Q_off_normalized[i, j] = Q[i, j] / total_off_diag
```

This normalization is executed in Phase 2 Step 2.5,
but documented as a Phase 1 deliverable alongside Q.

### Output

Generator matrix Q (4x4) satisfying diagonal condition $q_{ii} = -\sum_{j \neq i} q_{ij}$.
League-specific or pooled. Used in Phase 3 for matrix exponential $e^{Q \cdot \Delta t}$.

---

## Step 1.3: Learning Prematch Prior Parameter a (Machine Learning)

### Goal

Provide **initial estimates** of baseline intensity that reflect match-level strength difference.
These values initialize Step 1.4 joint optimization; final a is determined by NLL.

### Feature Architecture — 3-Tier Structure

Build features with three tiers made possible by Goalserve full package:

#### Tier 1: Team-Level Rolling Stats

**Source: Goalserve Live Game Stats — `stats.{team}`**

Aggregate rolling averages over each team's last five matches:

| Feature | Goalserve Field | Calculation |
|------|---------------|------|
| xG_per_90 | Live Game Stats xG field | xG / number of matches |
| xGA_per_90 | opponent xG | conceded-threat proxy |
| shots_per_90 | `stats.shots.total` | total shot frequency |
| shots_on_target_per_90 | `stats.shots.ongoal` | on-target shot frequency |
| shots_insidebox_ratio | `stats.shots.insidebox / stats.shots.total` | box penetration rate |
| possession_avg | `stats.possestiontime.total` | possession |
| pass_accuracy | `stats.passes.accurate / stats.passes.total` | passing accuracy |
| corners_per_90 | `stats.corners.total` | corner frequency |
| fouls_per_90 | `stats.fouls.total` | foul frequency (aggression proxy) |
| saves_per_90 | `stats.saves.total` | GK save frequency |

**Per-half split features (optional extension):**

Goalserve provides first/second-half split stats via `_h1`, `_h2` suffix:

| Feature | Meaning |
|------|------|
| shots_h2_ratio | second-half shot share -> stamina/tactical-change proxy |
| possession_h1_vs_h2 | first-vs-second-half possession gap -> game-management pattern |

#### Tier 2: Player-Level Aggregated Features

**Source: Goalserve Live Game Stats — `player_stats.{team}.player[]`**

Aggregate recent five-match stats of today's starting XI (confirmed in Phase 2) by position:

```python
def build_player_tier_features(starting_11_ids: List[str],
                                player_history: Dict) -> dict:
    """
    Historical stats of starting XI -> team-level aggregates.
    player_history: {player_id: [recent 5 matches of player_stats]}
    """
    features = {}

    for pos_group, pos_codes in [
        ("fw", ["F"]),
        ("mf", ["M"]),
        ("df", ["D"]),
        ("gk", ["G"])
    ]:
        players_in_group = [
            pid for pid in starting_11_ids
            if player_history[pid][0]["pos"] in pos_codes
        ]

        if not players_in_group:
            continue

        # Rolling metrics by position group
        ratings = []
        goals_p90 = []
        key_passes_p90 = []
        tackles_p90 = []

        for pid in players_in_group:
            for game_stats in player_history[pid]:
                mp = float(game_stats.get("minutes_played") or 0)
                if mp < 10:
                    continue  # Exclude too-short appearances

                ratings.append(float(game_stats.get("rating") or 0))
                goals_p90.append(
                    safe_float(game_stats.get("goals")) / mp * 90
                )
                key_passes_p90.append(
                    safe_float(game_stats.get("keyPasses")) / mp * 90
                )
                tackles_p90.append(
                    safe_float(game_stats.get("tackles")) / mp * 90
                )

        features[f"{pos_group}_avg_rating"] = safe_mean(ratings)
        features[f"{pos_group}_goals_p90"] = safe_sum(goals_p90)
        features[f"{pos_group}_key_passes_p90"] = safe_sum(key_passes_p90)
        features[f"{pos_group}_tackles_p90"] = safe_sum(tackles_p90)

    return features
```

**Core player aggregate features:**

| Feature | Position | Calculation | Meaning |
|------|--------|------|------|
| fw_avg_rating | FW | rolling mean rating | current attacking form |
| fw_goals_p90 | FW | sum(goals / minutes * 90) | attack scoring productivity |
| mf_key_passes_p90 | MF | sum(keyPasses / minutes * 90) | creativity |
| mf_pass_accuracy | MF | mean(passes_acc / passes) | build-up quality |
| df_tackles_p90 | DF | sum(tackles / minutes * 90) | defensive intensity |
| df_interceptions_p90 | DF | sum(interceptions / minutes * 90) | defensive positioning |
| gk_save_rate | GK | saves / (saves + goals_conceded) | GK performance |
| team_avg_rating | all players | minutes-weighted mean rating | overall team form |

> **minutes_played caution:** in Goalserve, unused bench players have empty `minutes_played`.
> Those entries should be excluded from rolling averages. Short substitute appearances (`mp < 10`) are also excluded as statistically unstable.

#### Tier 3: Odds Features

**Source: Goalserve Pregame Odds — 20+ bookmakers**

```python
def build_odds_features(bookmakers: List[dict]) -> dict:
    """20+ bookmaker odds -> feature vector."""

    def remove_overround(h, d, a):
        total = 1/h + 1/d + 1/a
        return (1/h)/total, (1/d)/total, (1/a)/total

    all_probs = []
    pinnacle_prob = None

    for bm in bookmakers:
        h = float(bm["odds"]["Home"])
        d = float(bm["odds"]["Draw"])
        a = float(bm["odds"]["Away"])
        prob = remove_overround(h, d, a)
        all_probs.append(prob)

        if bm["name"] == "Pinnacle":
            pinnacle_prob = prob

    # If Pinnacle is unavailable, use market average
    if pinnacle_prob is None:
        pinnacle_prob = tuple(np.mean(all_probs, axis=0))

    return {
        "pinnacle_home_prob": pinnacle_prob[0],
        "pinnacle_draw_prob": pinnacle_prob[1],
        "pinnacle_away_prob": pinnacle_prob[2],
        "market_avg_home_prob": np.mean([p[0] for p in all_probs]),
        "market_avg_draw_prob": np.mean([p[1] for p in all_probs]),
        "bookmaker_odds_std": np.std([p[0] for p in all_probs]),
        # Add if open/close available
        # "odds_movement": close_home - open_home,
    }
```

| Feature | Meaning |
|------|------|
| pinnacle_home/draw/away_prob | implied probabilities from most efficient market |
| market_avg_home_prob | consensus probability across 20+ bookmakers |
| bookmaker_odds_std | market uncertainty (higher std = harder match) |
| odds_movement | pre-kickoff information-flow direction |

#### Tier 4: Context Features

| Feature | Source | Calculation |
|------|------|------|
| home_away_flag | Fixtures | localteam/visitorteam indicator |
| rest_days | Fixtures date diff | days since previous match |
| h2h_goal_diff | Fixtures H2H | mean goal difference over last 5 H2H |

### Feature Selection

Using XGBoost built-in feature importance (gain):

$$\text{Importance}(f) = \sum_{\text{splits on } f} \Delta \mathcal{L}_{\text{Poisson}}$$

Select top d' features that reach 95% cumulative importance,
and store in `feature_mask.json`. Apply the same mask in Phase 2 inference.

> **Why not PCA:** PCA is linear projection and can mismatch with nonlinear models (XGBoost).
> XGBoost's Poisson-deviance importance aligns better with objective.

### Target (y)

Each team's **total goals** in that match (including stoppage-time goals, excluding VAR-cancelled goals).
Use either separate home/away models or a single model with home/away flag.

### Modeling

- XGBoost / LightGBM, objective: `count:poisson`
- Output: full-match expected goals $\hat{\mu}_H, \hat{\mu}_A$ for each team

### Converting to Initial a

$$a_H^{(init)} = \ln\!\left(\frac{\hat{\mu}_H}{T_m}\right), \quad a_A^{(init)} = \ln\!\left(\frac{\hat{\mu}_A}{T_m}\right)$$

This is an initial estimate under constant-intensity assumption; corrected jointly with b in Step 1.4.

### Output

- Trained XGBoost weights (`.xgb`)
- `feature_mask.json`
- Predictor that outputs $\hat{\mu}_H, \hat{\mu}_A$ for a new match feature vector

### Fallback by Feature Availability

Depending on Live Game Stats historical coverage:

| Coverage | Tier 2 (player-level) strategy |
|----------|----------------------|
| 5+ seasons | Apply Tier 2 over full period |
| 2-4 seasons | Tier 2 only for recent period, Tier 1 only elsewhere |
| <=1 season | Disable Tier 2, train with Tier 1 + Tier 3 only |

During trial period, verify backfill coverage of `player_stats`.

---

## Step 1.4: Joint NLL Optimization (MMPP Calibration)

### Goal

Jointly optimize time profile, red-card penalty, score-difference effect, and match-level baseline intensity.

### Resolving Circular Dependency

Correct conversion for a needs b:

$$a = \ln\!\left(\frac{\hat{\mu} \cdot b}{e^{bT} - 1}\right) \quad \leftarrow \text{requires b (circular reference)}$$

**Solution:** treat a as learnable parameters instead of fixed constants,
and jointly minimize NLL with b, gamma, delta.
Add regularization pulling a toward ML predictions to prevent overfitting.

### Time Basis Functions (Piecewise Basis)

$$\sum_{i=1}^{K} b_i \cdot B_i(t), \quad K = 6$$

| i | $B_i(t)$ | Covered interval |
|---|----------|----------|
| 1 | $\mathbb{1}_{[0, 15)}(t)$ | early first half |
| 2 | $\mathbb{1}_{[15, 30)}(t)$ | mid first half |
| 3 | $\mathbb{1}_{[30, 45+\alpha_1)}(t)$ | late first half + first-half stoppage |
| 4 | $\mathbb{1}_{[HT_{end}, HT_{end}+15)}(t)$ | early second half |
| 5 | $\mathbb{1}_{[HT_{end}+15, HT_{end}+30)}(t)$ | mid second half |
| 6 | $\mathbb{1}_{[HT_{end}+30, T_m)}(t)$ | late second half + second-half stoppage |

Since halftime is included in no basis function, it is automatically excluded from integration.

> **If using $t_{eff}$ transform:** basis can be simplified to $B_i = \mathbb{1}_{[15(i-1), 15i)}$.

**Sanity check for b via per-half stats (Goalserve-specific advantage):**

Goalserve Live Game Stats provides `_h1`, `_h2` split shots/passes.
Check whether learned first-vs-second-half weight in b[1..6] roughly matches empirical shot split:

$$\frac{\exp(b_1) + \exp(b_2) + \exp(b_3)}{\sum_{i=1}^{6} \exp(b_i)} \approx \frac{\text{shots.total\_h1}}{\text{shots.total}} \quad \text{(league average)}$$

### Red-Card Penalty gamma — Team-Specific Separation

Home and away teams use **separate gamma**.
A red card harms one side and benefits the other, so shared gamma is invalid.

**Home-team gamma^H:**

$$\gamma^H = [0,\; \gamma^H_1,\; \gamma^H_2,\; \gamma^H_1 + \gamma^H_2]$$

| State | $\gamma^H$ | Physical interpretation |
|------|-----------|------------|
| 0 (11v11) | 0 | reference point |
| 1 (home dismissed) | $\gamma^H_1 < 0$ | home numerical disadvantage -> home scoring **decreases** |
| 2 (away dismissed) | $\gamma^H_2 > 0$ | home numerical advantage -> home scoring **increases** |
| 3 (both dismissed) | $\gamma^H_1 + \gamma^H_2$ | additive composition |

**Away-team gamma^A:**

$$\gamma^A = [0,\; \gamma^A_1,\; \gamma^A_2,\; \gamma^A_1 + \gamma^A_2]$$

| State | $\gamma^A$ | Physical interpretation |
|------|-----------|------------|
| 0 (11v11) | 0 | reference point |
| 1 (home dismissed) | $\gamma^A_1 > 0$ | away numerical advantage -> away scoring **increases** |
| 2 (away dismissed) | $\gamma^A_2 < 0$ | away numerical disadvantage -> away scoring **decreases** |
| 3 (both dismissed) | $\gamma^A_1 + \gamma^A_2$ | additive composition |

**Optional symmetry constraints:**

$$\gamma^A_1 = -\gamma^H_2, \quad \gamma^A_2 = -\gamma^H_1$$

Free parameters: 4 (asymmetric) or 2 (symmetric). Compare empirically in Step 1.5.

### Score-Dependent Intensity delta(ΔS)

| ΔS | Home $\delta_H$ | Away $\delta_A$ | Interpretation |
|----|----------------|-------------------|------|
| <= -2 | $\delta_H^{(-2)}$ | $\delta_A^{(-2)}$ | home trailing heavily |
| -1 | $\delta_H^{(-1)}$ | $\delta_A^{(-1)}$ | home trailing slightly |
| 0 | 0 (fixed) | 0 (fixed) | **reference point** |
| +1 | $\delta_H^{(+1)}$ | $\delta_A^{(+1)}$ | home slightly ahead |
| >= +2 | $\delta_H^{(+2)}$ | $\delta_A^{(+2)}$ | home comfortably ahead |

- Fix reference: delta(0) = 0 for identifiability
- Merge |ΔS| >= 3 into |ΔS| = 2 bucket (data sparsity)
- Free parameters: home 4 + away 4 = 8 (or 4 with symmetry)

### Interval Integral (Closed-Form) — Home/Away Separate

In interval k where $(X_k, \Delta S_k)$ is constant and basis index is $i_k$:

$$\mu^H_k = \exp\!\left(a^m_H + b_{i_k} + \gamma^H_{X_k} + \delta_H(\Delta S_k)\right) \cdot (t_k - t_{k-1})$$

$$\mu^A_k = \exp\!\left(a^m_A + b_{i_k} + \gamma^A_{X_k} + \delta_A(\Delta S_k)\right) \cdot (t_k - t_{k-1})$$

### Point-Event Contribution (Goal Times) — Home/Away Separate

Home goal: $\ln \lambda_H(t_g) = a^m_H + b_{i(t_g)} + \gamma^H_{X(t_g)} + \delta_H(\Delta S_{before,g})$

Away goal: $\ln \lambda_A(t_g) = a^m_A + b_{i(t_g)} + \gamma^A_{X(t_g)} + \delta_A(\Delta S_{before,g})$

> **Own goals:** excluded from point-event terms (per Step 1.1 policy).
> Included in interval integral through ΔS updates.

### Loss Function (Final NLL)

$$\mathcal{L} = \underbrace{-\sum_{m=1}^{M}\Bigg[\sum_{g \in \text{HomeGoals}_m} \ln \lambda_H(t_g) + \sum_{g \in \text{AwayGoals}_m} \ln \lambda_A(t_g) - \sum_{k \in \text{Intervals}_m} \left(\mu^H_k + \mu^A_k\right)\Bigg]}_{\text{Negative Log-Likelihood}}$$

$$+ \underbrace{\frac{1}{2\sigma_a^2}\sum_{m=1}^M \left[(a^m_H - a^{m,(init)}_H)^2 + (a^m_A - a^{m,(init)}_A)^2\right]}_{\text{ML Prior Regularization}}$$

$$+ \underbrace{\lambda_{reg}\left(\|\mathbf{b}\|^2 + \|\boldsymbol{\gamma}^H\|^2 + \|\boldsymbol{\gamma}^A\|^2 + \|\boldsymbol{\delta}_H\|^2 + \|\boldsymbol{\delta}_A\|^2\right)}_{\text{L2 Regularization}}$$

> **Exclude own goals from HomeGoals/AwayGoals.**
> **Exclude VAR-cancelled goals.** `var_cancelled=True` goals are already filtered in Step 1.1.

### Learnable Parameters (PyTorch `nn.Parameter`)

| Parameter | Dimension | Init | Note |
|---------|------|--------|------|
| $a^m_H$ | M x 1 | $\ln(\hat{\mu}^m_H / T_m)$ | match-level home baseline intensity |
| $a^m_A$ | M x 1 | $\ln(\hat{\mu}^m_A / T_m)$ | match-level away baseline intensity |
| **b** | 6 x 1 | **0** | time-profile by interval |
| $\gamma^H_1, \gamma^H_2$ | 2 scalars | 0, 0 | home-team red-card penalty |
| $\gamma^A_1, \gamma^A_2$ | 2 scalars | 0, 0 | away-team red-card penalty |
| $\boldsymbol{\delta}_H$ | 4 x 1 | **0** | home score-difference effect |
| $\boldsymbol{\delta}_A$ | 4 x 1 | **0** | away score-difference effect |

Total free parameters: $2M + 6 + 4 + 8 = 2M + 18$
(with gamma symmetry: $2M + 16$, with additional delta symmetry: $2M + 12$)

### Parameter Clamping

| Parameter | Allowed Range | Physical rationale |
|---------|----------|------------|
| $b_i$ | [-0.5, 0.5] | interval intensity ratio above x1.65 is unrealistic |
| $\gamma^H_1$ | [-1.5, 0] | home dismissal -> home scoring down |
| $\gamma^H_2$ | [0, 1.5] | away dismissal -> home scoring up |
| $\gamma^A_1$ | [0, 1.5] | home dismissal -> away scoring up |
| $\gamma^A_2$ | [-1.5, 0] | away dismissal -> away scoring down |
| $\delta_H^{(-2)}, \delta_H^{(-1)}$ | [-0.5, 1.0] | trailing home tends to attack more |
| $\delta_H^{(+1)}, \delta_H^{(+2)}$ | [-1.0, 0.5] | leading home tends to protect lead |
| $\delta_A^{(-2)}, \delta_A^{(-1)}$ | [-1.0, 0.5] | leading away tends to protect lead |
| $\delta_A^{(+1)}, \delta_A^{(+2)}$ | [-0.5, 1.0] | trailing away tends to attack more |

### Optimization Strategy

**1. Multi-start:**
Since NLL is non-convex, initialize b, gamma, delta from
5-10 random seeds and choose best local minimum.

**2. Two-stage optimizer:**
Adam (lr=1e-3, 1000 epochs) -> L-BFGS (fine-tuning).

**3. Numerical stability:**
With piecewise basis intervals, b -> 0 singularity does not arise.

### Output

- Time-interval scoring profile $\mathbf{b} = [b_1, \ldots, b_6]$
- Home red-card penalty $\gamma^H_1, \gamma^H_2$ (+ $\gamma^H_3 = \gamma^H_1 + \gamma^H_2$)
- Away red-card penalty $\gamma^A_1, \gamma^A_2$ (+ $\gamma^A_3 = \gamma^A_1 + \gamma^A_2$)
- Score-difference effects $\boldsymbol{\delta}_H, \boldsymbol{\delta}_A$
- Corrected match-level baselines $\{a^m_H, a^m_A\}$

---

## Step 1.5: Time-Series Cross-Validation and Model Diagnostics (Validation)

### Goal

Detect overfitting and quantify probabilistic prediction quality.
**If this step fails, do not deploy to live.**

### Walk-Forward Validation

| Fold | Train Period | Validation Period |
|------|----------|----------|
| 1 | Seasons 1-3 | Season 4 |
| 2 | Seasons 1-4 | Season 5 |
| 3 | Seasons 2-5 | Season 6 |

For each fold, run Step 1.3 (ML) and Step 1.4 (NLL) using train period only,
then measure metrics below on validation period.

### Core Diagnostic Metrics

**1. Calibration Plot (Reliability Diagram):**

Visualize whether events predicted at "P = 0.6" actually occur near 60%.

**2. Brier Score — Pinnacle baseline:**

Use Goalserve Pregame Odds **Pinnacle close line** as precise market benchmark:

$$BS_{model} = \frac{1}{N}\sum_n (P_{model,n} - O_n)^2$$

$$BS_{pinnacle} = \frac{1}{N}\sum_n (P_{pinnacle\_close,n} - O_n)^2$$

$$\Delta BS = BS_{model} - BS_{pinnacle}$$

If $\Delta BS < 0$, model beats the most efficient market (Pinnacle).

> **Goalserve-specific advantage:** historical close odds from 20+ bookmakers,
> enabling baselines beyond Pinnacle (bet365, Marathonbet, etc.).

**3. Log Loss (validation NLL):**

$$\text{Log Loss} = -\frac{1}{N}\sum_{n=1}^N [O_n \ln P_n + (1-O_n)\ln(1-P_n)]$$

**4. Simulation P&L:**

Because historical Goalserve Pregame Odds are available,
proxy Kalshi quotes with Pinnacle odds and backtest through Phase 4 Kelly logic.

**5. Multi-market cross-validation (Goalserve-specific):**

Since Goalserve Pregame Odds provides 50+ markets,
validate probabilities implied by model μ_H, μ_A **across multiple markets at once**:

| Market | Derived from Model | Derived from Market | Comparison |
|------|-------------|-------------|------|
| Match Winner | Poisson(μ_H) vs Poisson(μ_A) | Pregame Odds 1X2 | Brier score |
| Over/Under 2.5 | 1 - CDF(2, μ_H + μ_A) | Pregame Odds O/U | Brier score |
| Both Teams to Score | composite Poisson | Pregame Odds BTTS | Brier score |

If model is good on 1X2 but poor on O/U, total μ may be right but split μ_H vs μ_A may be wrong.
μ_H and μ_A should perform well jointly across all markets.

### gamma Sign Validation

| Expected Sign | Validation |
|----------|------|
| $\gamma^H_1 < 0$ | home dismissal -> home scoring down |
| $\gamma^H_2 > 0$ | away dismissal -> home scoring up |
| $\gamma^A_1 > 0$ | home dismissal -> away scoring up |
| $\gamma^A_2 < 0$ | away dismissal -> away scoring down |

**Symmetric vs asymmetric gamma comparison:**

- Symmetric model: $\gamma^A_1 = -\gamma^H_2$, $\gamma^A_2 = -\gamma^H_1$ (2 parameters)
- Asymmetric model: independent 4 parameters

Compare validation Log Loss to justify asymmetry.

### delta Sign Validation

| Expected Sign | Validation |
|----------|------|
| $\delta_H^{(-1)} > 0$ | trailing home attacks more |
| $\delta_H^{(+1)} < 0$ | leading home shifts defensive |
| $\delta_A^{(-1)} < 0$ | leading away shifts defensive |
| $\delta_A^{(+1)} > 0$ | trailing away attacks more |

**Test rejection of delta = 0 (Likelihood Ratio Test):**

$$LR = -2(\mathcal{L}_{\delta=0} - \mathcal{L}_{\delta \neq 0}) \sim \chi^2(df)$$

If p < 0.05, inclusion of delta is justified. If not rejected, keep delta-free model.

**Symmetric vs asymmetric delta comparison:**

- Symmetric: $\delta_A(\Delta S) = \delta_H(-\Delta S)$ (4 parameters)
- Asymmetric: independent 8 parameters

### b Validation — per-half stats cross-check (Goalserve-specific)

Compare learned first/second-half weight in b[1..6] with actual shot split from Goalserve:

```python
def validate_b_with_half_stats(b, stats_db):
    """
    Cross-validate learned half split in b against
    shots.total_h1 and shots.total_h2 from Goalserve Live Game Stats.
    """
    # Model first-half weight
    model_h1_weight = sum(np.exp(b[i]) for i in range(3))
    model_h2_weight = sum(np.exp(b[i]) for i in range(3, 6))
    model_h1_ratio = model_h1_weight / (model_h1_weight + model_h2_weight)

    # Empirical first-half shot share (league average)
    actual_h1_ratio = stats_db["shots_h1_total"] / stats_db["shots_total"]

    discrepancy = abs(model_h1_ratio - actual_h1_ratio)
    if discrepancy > 0.10:
        log.warning(f"b half-ratio mismatch: model={model_h1_ratio:.2f}, "
                    f"actual={actual_h1_ratio:.2f}")
```

### Pass Criteria (Go/No-Go)

| Criterion | Threshold |
|------|--------|
| Calibration plot | within +/-5% of diagonal |
| Brier Score | $\Delta BS < 0$ (improve vs Pinnacle) |
| Multi-market BS | improve vs market in 1X2, O/U, BTTS |
| Simulated Max Drawdown | <= 20% of capital |
| All folds | positive simulated return in all 3 folds |
| gamma signs | all 4 align with football intuition |
| delta signs | align with football intuition |
| b half split | within +/-10% of empirical shot split |

### Output

Fix the final parameter set that passes all criteria as **production parameters** and hand off to Phase 2.

---

## Phase 1 -> Phase 2 Handoff

| Parameter | Source | Usage |
|---------|------|------|
| XGBoost weights + `feature_mask.json` | Step 1.3 | predict $\hat{\mu}_H, \hat{\mu}_A$ for new matches |
| $\mathbf{b} = [b_1, \ldots, b_6]$ | Step 1.4 | time-interval scoring profile |
| $\gamma^H_1, \gamma^H_2$ | Step 1.4 | home intensity jump under dismissals |
| $\gamma^A_1, \gamma^A_2$ | Step 1.4 | away intensity jump under dismissals |
| $\boldsymbol{\delta}_H, \boldsymbol{\delta}_A$ | Step 1.4 | score-difference intensity adjustment |
| Q (4x4 matrix) | Step 1.2 | future dismissal probabilities (matrix exponential) |
| $\mathbb{E}[\alpha_1], \mathbb{E}[\alpha_2]$ | Step 1.1 | compute $T_{exp}$ in Phase 2 |
| delta significance flag (`DELTA_SIGNIFICANT`) | Step 1.5 LRT | choose analytic/MC mode in Phase 3 |
| Pinnacle BS baseline | Step 1.5 | market benchmark for Phase 4 post-analysis |

> **delta and Phase 2:** at kickoff, ΔS = 0 so delta(0) = 0.
> Therefore delta does not affect a back-solving in Phase 2.
> delta activates only after goals occur in Phase 3.

---

## Phase 1 Pipeline Summary

```
[Goalserve Full Package: 5+ Seasons, 500+ Leagues]
              |
              v
+---------------------------------------------------------------+
|  Step 1.1: Interval Segmentation (Data Engineering)            |
|  • Fixtures/Results -> goals (VAR filtering) + red-card events |
|  • addedTime_period1/2 -> match-level T_m                      |
|  • var_cancelled=True -> exclude, owngoal -> exclude point term|
|  • Tag each interval with (X, ΔS), store ΔS_before + scorer    |
|  Output: intervals[], home_goal_events[], away_goal_events[]   |
+------------------+--------------------------------------------+
                   |
        +----------+----------+
        v                     v
+--------------+    +------------------------------------------+
|  Step 1.2:   |    |  Step 1.3: ML Prior (XGBoost)            |
|  Estimate Q  |    |  • Tier 1: team rolling stats (incl. xG) |
|  • Fixtures  |    |  • Tier 2: player aggregates (rating...) |
|    redcards  |    |  • Tier 3: odds (20+ bookmakers)         |
|  • Empirical |    |  • Tier 4: context (H/A, rest, H2H)      |
|    rates     |    |  • Feature selection via importance       |
|  • gamma^H/A |    |  • Poisson regression -> μ̂_H, μ̂_A      |
|    additive  |    |  Output: â_H^(init), â_A^(init), .xgb     |
|  • League    |    +--------------+---------------------------+
|    stratified|                   |
+------+-------+                   |
       |                           |
       +-----------+---------------+
                   v
+---------------------------------------------------------------+
|  Step 1.4: Joint NLL Optimization (PyTorch)                    |
|  • Jointly learn a^m_H, a^m_A, b[1..6], gamma^H_1/2,         |
|    gamma^A_1/2, delta_H[4], delta_A[4]                        |
|  • Home/away separated goal NLL                                |
|  • Exclude own goals from point-event term                     |
|  • Multi-start + L2 regularization + clamping                  |
|  Output: b[], gamma^H, gamma^A, delta_H[], delta_A[],         |
|          {a^m_H, a^m_A}                                        |
+------------------+--------------------------------------------+
                   v
+---------------------------------------------------------------+
|  Step 1.5: Time-Series Cross-Validation (Validation)           |
|  • Walk-forward CV (prevent temporal leakage)                  |
|  • Brier Score vs Pinnacle close line (Goalserve Odds)         |
|  • Multi-market checks (1X2 + O/U + BTTS)                      |
|  • b half split vs empirical shot split (per-half stats)       |
|  • gamma sign checks (4), delta sign checks, LRT               |
|  • gamma/delta symmetric vs asymmetric comparisons              |
|  • Simulated P&L + Max Drawdown                                |
|  • Go/No-Go decision                                            |
|  Output: Production Parameters + DELTA_SIGNIFICANT flag        |
+---------------------------------------------------------------+
```
