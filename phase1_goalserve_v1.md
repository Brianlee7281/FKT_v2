# Phase 1: Offline Calibration — Goalserve Full Package

## 개요

과거 데이터로부터 MMPP(Markov-Modulated Poisson Process)의 모든 파라미터를 학습하는 단계.
이 단계가 부실하면 라이브 트레이딩의 모든 계산이 Garbage In, Garbage Out이 된다.

Goalserve 풀 패키지에 축적된 수만 경기의 데이터를 가져와,
수학적 모델의 파라미터들을 정교하게 추출해 내는 과정을 5개의 선형적 Step으로 분해한다.

### 데이터 소스 일원화

Phase 1~4 전체를 **Goalserve 단일 소스**로 통일한다.
스키마 불일치, ID 매핑 오류가 원천적으로 사라진다.

| Goalserve 패키지 | Phase 1 역할 | 핵심 데이터 |
|-----------------|-------------|------------|
| **Fixtures/Results** | 구간 분할 + 이벤트 타임라인 | 골(분+VAR), 레드카드(분), 추가시간, 하프타임 스코어, 라인업 |
| **Live Game Stats** | 팀/선수 스탯 + xG | per-half 팀 스탯, 선수별 상세 스탯(rating, passes, shots 등), xG |
| **Pregame Odds** | 배당률 피처 + 시장 기준선 | 20+ 북메이커 odds(open/close), 50+ 마켓 |

### 득점 강도 함수 (최종 형태)

홈팀과 어웨이팀이 **별도의 γ, δ**를 사용한다.

$$\lambda_H(t \mid X, \Delta S) = \exp\!\left(a_H + b_{i(t)} + \gamma^H_{X(t)} + \delta_H(\Delta S(t))\right)$$

$$\lambda_A(t \mid X, \Delta S) = \exp\!\left(a_A + b_{i(t)} + \gamma^A_{X(t)} + \delta_A(\Delta S(t))\right)$$

| 기호 | 의미 | 추정 Step |
|------|------|----------|
| $a_H, a_A$ | 경기별 기본 득점 강도 (팀 전력) | Step 1.3 초기값 → Step 1.4 보정 |
| $b_{i(t)}$ | 시간 구간별 득점 빈도 프로파일 | Step 1.4 |
| $\gamma^H_{X(t)}$ | 레드카드 상태 → **홈팀** 득점 패널티 | Step 1.4 |
| $\gamma^A_{X(t)}$ | 레드카드 상태 → **어웨이팀** 득점 패널티 | Step 1.4 |
| $\delta_H(\Delta S)$ | 스코어차 → 홈팀 전술 효과 | Step 1.4 |
| $\delta_A(\Delta S)$ | 스코어차 → 어웨이팀 전술 효과 | Step 1.4 |

---

## Input Data

Goalserve 풀 패키지의 3개 API로부터:

**1. Fixtures/Results — 과거 5시즌+, 500+ 리그:**

```
GET /getfeed/{api_key}/soccerfixtures/league/{league_id}?json=1
```

- 경기별 이벤트 타임라인: `summary.{team}.goals`, `summary.{team}.redcards`, `summary.{team}.yellowcards`
- 라인업: `teams.{team}.player[]` (formation_pos, pos, id)
- 교체: `substitutions.{team}.substitution[]`
- 경기 메타: `matchinfo.time.addedTime_period1/2`, `{team}.ht_score`, `{team}.ft_score`
- 상태: `status` (Full-time, Postponed, Cancelled 등)

**2. Live Game Stats — 과거 경기 상세 스탯 (100+ 리그):**

```
GET /getfeed/{api_key}/soccerstats/match/{match_id}?json=1
```

- 팀 스탯: `stats.{team}` — shots, passes, possession, corners, fouls, saves (per-half)
- 선수 스탯: `player_stats.{team}.player[]` — rating, goals, assists, shots, passes, tackles, interceptions, minutes_played 등
- xG: Expected Goals (Live Game Stats 패키지에 포함)

**3. Pregame Odds — 과거 배당률 (500+ 리그, 20+ 북메이커):**

```
GET /getfeed/{api_key}/soccernew/{league_id}?json=1
```

- 북메이커별 배당: `bookmaker[].odd[]` (name, value)
- 50+ 마켓: Match Winner, Over/Under, Asian Handicap 등
- Historical odds: open/close 배당 포함

---

## Step 1.1: 시계열 이벤트 분할 및 구간 데이터화 (Data Engineering)

### 목표

과거 경기의 점(Point) 이벤트를 **λ가 일정한 연속 구간(Interval)**으로 변환한다.

강도 함수 λ는 $(X(t), \Delta S(t))$에 의존하므로,
이 두 변수 중 하나라도 변하는 시점에서 구간을 잘라야 한다.

### Goalserve 데이터 매핑

**골 이벤트 — `summary.{team}.goals.player[]`:**

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

| 필드 | 용도 |
|------|------|
| `minute` + `extra_min` | 골 발생 시각 (추가시간 골: `minute`=90, `extra_min`=3 → 93분) |
| `{team}` 키 (localteam/visitorteam) | 득점 팀 식별 → NLL에서 ln λ_H vs ln λ_A 분기 |
| `owngoal` | True이면 득점 팀을 반전 (자책골은 상대 팀 스코어 증가) |
| **`var_cancelled`** | **True이면 해당 골을 구간 분할에서 완전 제외** |
| `penalty` | 로깅/분석용 (페널티킥 여부) |

> **VAR 취소골 처리 — 원래 설계에 없던 중요한 추가:**
> `var_cancelled = "True"`인 골은 실제로 ΔS를 변경하지 않았으므로
> 구간 분할에서 완전히 제외해야 한다. 이걸 무시하면 ΔS가 오염되어
> δ 추정이 체계적으로 편향된다.

**레드카드 이벤트 — `summary.{team}.redcards.player[]`:**

```json
{
  "id": "...",
  "minute": "35",
  "extra_min": "",
  "name": "Player Name"
}
```

| 필드 | 용도 |
|------|------|
| `minute` + `extra_min` | 퇴장 시각 |
| `{team}` 키 | 퇴장 팀 → X(t) 전이 방향 결정 |

> **두 번째 옐로카드 퇴장 확인:**
> `summary.redcards`에 두 번째 옐로 퇴장이 포함되는지 trial 기간에 검증한다.
> 미포함 시, `player_stats.{team}.player[].redcards` 필드(선수별)와 교차 확인하여 보완한다.

**추가시간 — `matchinfo.time`:**

```json
{
  "addedTime_period1": "7",
  "addedTime_period2": "8"
}
```

경기별 실제 종료 시간:

$$T_m = 90 + \alpha_1 + \alpha_2$$

전반/후반 추가시간이 별도 필드로 제공되므로, "마지막 플레이 시각 추정" 같은 모호한 방법이 불필요하다.

**하프타임 — `{team}.ht_score`:**

```json
"localteam": { "ht_score": "2", "ft_score": "3" },
"visitorteam": { "ht_score": "0", "ft_score": "3" }
```

전반 종료 시점의 정확한 스코어를 알 수 있으므로, 하프타임 경계의 ΔS를 확실히 결정할 수 있다.

### 구간 경계(Split Point) 규칙

| 이벤트 | 분할 여부 | 이유 |
|--------|----------|------|
| 골 (`var_cancelled=False`) | ✅ | ΔS 변경 → δ 변경 |
| 골 (`var_cancelled=True`) | **❌** | ΔS 미변경 — 취소골 |
| 레드카드 | ✅ | X(t) 변경 → γ 변경 |
| 하프타임 시작 | ✅ | 적분에서 제외 |
| 하프타임 종료 | ✅ | 적분 재개 |
| 경기 종료 | ✅ | 구간 닫기 |
| 옐로카드, 교체 | ❌ | 현재 모델에서 상태 변수에 미포함 |

### 하프타임 처리

하프타임 동안은 λ(t) = 0이므로, 이 구간이 적분에 포함되면
"아무 이벤트도 없는 긴 시간"이 시간 프로파일 b_i 추정치를 왜곡한다.

**실질 플레이 시간 변환:**

$$t_{eff} = \begin{cases} t & \text{if } t < 45 + \alpha_1 \\ t - \delta_{HT} & \text{if } t \geq 45 + \alpha_1 + \delta_{HT} \end{cases}$$

$\alpha_1$: 전반 추가시간 (`addedTime_period1`),
$\delta_{HT}$: 하프타임 휴식 길이 (약 15분).

하프타임 구간은 별도 플래그로 표시하고 NLL 적분에서 완전히 제외한다.

### 골의 이중 역할

δ(ΔS) 도입으로 골은 두 가지 역할을 동시에 수행한다:

1. **구간 경계:** 골 직후 새 구간이 시작되며, 새 구간의 δ는 골 이후의 ΔS를 적용
2. **점 이벤트:** NLL의 Σ ln λ(t_i) 항에 기여. 이때 δ는 **골 직전**의 ΔS를 사용

> **인과관계 주의:** 0-0에서 홈이 골을 넣었을 때, 그 골의 λ 기여분에
> δ(+1)을 적용하면 "앞서는 상황에서의 득점력"을 반영하게 되어
> 인과관계가 역전된다. 골 시점의 NLL 기여에는 반드시 **골 직전** ΔS를 사용한다.

### 자책골 처리

`owngoal = "True"`인 골은 **기록상 팀과 실제 득점 팀이 반대**이다:

```python
def resolve_scoring_team(goal_event, recorded_team):
    """자책골이면 득점 팀을 반전"""
    if goal_event["owngoal"] == "True":
        return "visitorteam" if recorded_team == "localteam" else "localteam"
    return recorded_team
```

자책골은 상대팀의 λ가 아니라 **외생적 확률 이벤트**이므로,
NLL의 점 이벤트 항에서 어떤 팀의 ln λ에 기여할지가 모호하다.

**처리 방침:**
- 자책골은 점 이벤트 항(Σ ln λ)에서 **제외**한다.
- 구간 적분 항(Σ μ_k)에는 정상 포함 (ΔS는 실제 스코어 변동 반영).
- 즉, 자책골은 "스코어를 바꾸지만 어떤 팀의 득점력도 증명하지 않는 이벤트"로 취급한다.

> **근거:** λ_H는 "홈팀이 의도적으로 골을 넣을 강도"를 모델링한다.
> 어웨이 수비수의 자책골은 이 강도에 포함되지 않는다.
> 자책골을 ln λ_H에 포함하면 λ_H가 과대 추정되어 a_H에 편향이 생긴다.

### 데이터 변환 예시

**Goalserve 원시 데이터 (2022 월드컵 결승 기준):**

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

**변환 결과 (T_m = 90 + 7 + 8 = 105, 연장전 포함 시 120+):**

| 구간 | 시간 범위 | X | ΔS | δ | 점 이벤트 | 득점 팀 |
|------|----------|---|-----|---|----------|---------|
| 1 | [0, 23) | 0 | 0 | δ(0)=0 | — | — |
| 2 | [23, 36) | 0 | +1 | δ(+1) | t=23, δ_before=δ(0) | **Home** |
| 3 | [36, 45+7) | 0 | +2 | δ(+2) | t=36, δ_before=δ(+1) | **Home** |
| — | HT | — | — | — | **하프타임: 적분 제외** | — |
| 4 | [HT_end, 80) | 0 | +2 | δ(+2) | — | — |
| 5 | [80, 81) | 0 | +1 | δ(+1) | t=80, δ_before=δ(+2) | **Away** |
| 6 | [81, 90+8) | 0 | 0 | δ(0)=0 | t=81, δ_before=δ(+1) | **Away** |
| ... | 연장전 계속 | | | | | |

### 구간 레코드 스키마

```python
@dataclass
class IntervalRecord:
    match_id: str           # Goalserve match ID (전 Phase 통일)
    t_start: float          # 구간 시작 (실질 플레이 시간)
    t_end: float            # 구간 종료
    state_X: int            # 마르코프 상태 {0,1,2,3}
    delta_S: int            # 스코어차 (홈 - 어웨이)
    home_goal_times: list   # 이 구간 내 홈 골 시각들
    away_goal_times: list   # 이 구간 내 어웨이 골 시각들
    goal_delta_before: list # 각 골의 직전 ΔS
    T_m: float              # 경기 실제 종료 시간
    is_halftime: bool       # 하프타임 구간 여부
    alpha_1: float          # 전반 추가시간 (addedTime_period1)
    alpha_2: float          # 후반 추가시간 (addedTime_period2)
```

### ETL 파이프라인

```python
def build_intervals_from_goalserve(match_data: dict) -> List[IntervalRecord]:
    """Goalserve Fixtures/Results → 구간 레코드 변환"""
    
    # 1. 추가시간 추출
    alpha_1 = float(match_data["matchinfo"]["time"]["addedTime_period1"] or 0)
    alpha_2 = float(match_data["matchinfo"]["time"]["addedTime_period2"] or 0)
    T_m = 90 + alpha_1 + alpha_2
    
    # 2. 이벤트 수집 + VAR 취소 필터링
    events = []
    
    for team_key in ["localteam", "visitorteam"]:
        goals = match_data["summary"][team_key].get("goals", {})
        if goals:
            for g in ensure_list(goals.get("player", [])):
                if g.get("var_cancelled") == "True":
                    continue  # VAR 취소골 제외
                
                scoring_team = resolve_scoring_team(g, team_key)
                minute = parse_minute(g["minute"], g.get("extra_min", ""))
                events.append(Event("goal", minute, scoring_team, g))
        
        redcards = match_data["summary"][team_key].get("redcards", {})
        if redcards:
            for r in ensure_list(redcards.get("player", [])):
                minute = parse_minute(r["minute"], r.get("extra_min", ""))
                events.append(Event("red_card", minute, team_key, r))
    
    # 하프타임 경계 추가
    events.append(Event("halftime_start", 45 + alpha_1, None, None))
    events.append(Event("halftime_end", 45 + alpha_1 + 15, None, None))  # ~15분 휴식
    events.append(Event("match_end", T_m, None, None))
    
    # 3. 시간순 정렬 후 구간 분할
    events.sort(key=lambda e: e.minute)
    intervals = split_into_intervals(events, T_m, alpha_1, alpha_2)
    
    return intervals
```

### 결과물

수만 경기가 수십만 개의 구간 레코드로 변환된다.
모든 레코드에 Goalserve match_id가 태그되어, 이후 Phase에서 동일 ID로 조회 가능.

---

## Step 1.2: 마르코프 체인 생성 행렬 Q의 추정 (Empirical + Shrinkage)

### 목표

레드카드 발생률(상태 전이율)을 과거 데이터에서 추정하여 4×4 생성 행렬 Q를 구성한다.

### 상태 공간

| 상태 | 의미 |
|------|------|
| 0 | 11v11 (평상시) |
| 1 | 10v11 (홈 퇴장) |
| 2 | 11v10 (어웨이 퇴장) |
| 3 | 10v10 (양팀 퇴장) |

### Goalserve 데이터 매핑

**레드카드 이벤트 타임라인 — Fixtures/Results `summary.{team}.redcards`:**

각 레드카드의 정확한 분(minute)과 대상 팀(localteam/visitorteam)을 알 수 있으므로,
경기별 마르코프 상태 경로를 완전히 복원할 수 있다:

```python
def reconstruct_markov_path(match_data: dict) -> List[Tuple[float, int]]:
    """경기의 마르코프 상태 경로 복원: [(시각, 상태), ...]"""
    path = [(0, 0)]  # 킥오프: 상태 0 (11v11)
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
            if current_state == 0: current_state = 1      # 11v11 → 10v11
            elif current_state == 2: current_state = 3    # 11v10 → 10v10
        else:
            if current_state == 0: current_state = 2      # 11v11 → 11v10
            elif current_state == 1: current_state = 3    # 10v11 → 10v10
        path.append((minute, current_state))
    
    return path
```

**교차 검증 — Live Game Stats `player_stats.{team}.player[].redcards`:**

Fixtures/Results의 summary.redcards와 Live Game Stats의 선수별 redcards 필드를 비교하여
데이터 누락(특히 두 번째 옐로 퇴장)이 없는지 확인한다.

### 기본 추정

$$q_{ij} = \frac{N_{ij}}{\sum_m \int_0^{T_m} \mathbb{1}_{\{X_m(t) = i\}}\, dt}$$

- 분자 $N_{ij}$: 전체 데이터에서 상태 i → j 전이가 관측된 횟수
- 분모: 전체 데이터에서 상태 i로 플레이된 **실질 플레이 시간**의 합
  - 하프타임 제외
  - 경기별 $T_m = 90 + \alpha_1 + \alpha_2$ 적용 (Goalserve `addedTime` 사용)
- 대각 성분: $q_{ii} = -\sum_{j \neq i} q_{ij}$

### 희소 상태 처리 (상태 3: 10v10)

가산 가정(Additivity Assumption):

$$q_{1 \to 3} \approx q_{0 \to 2}, \quad q_{2 \to 3} \approx q_{0 \to 1}$$

득점 패널티도 팀별로 가산:

$$\gamma^H_3 = \gamma^H_1 + \gamma^H_2, \quad \gamma^A_3 = \gamma^A_1 + \gamma^A_2$$

### 리그별 층화 추정

Goalserve Fixtures/Results는 500+ 리그를 커버하므로, 리그별 Q를 독립 추정할 데이터가 충분하다.

- **옵션 A — 리그별 개별 Q:** Kalshi에서 거래 가능한 리그(EPL, La Liga, Bundesliga, Serie A, Ligue 1)는 각각 독립 추정
- **옵션 B — 계층적 베이지안:** 전체 리그 풀에서 사전분포를 설정하고, 개별 리그로 사후분포 업데이트. 데이터가 적은 리그에 유리

### Q_off 정규화 (MC 시뮬레이션용)

Phase 3 Step 3.4의 Monte Carlo에서 "퇴장 이벤트 발생 시 어떤 상태로 전이하는가"를
결정할 때, Q의 비대각 성분을 **전이 확률**로 정규화해야 한다:

```python
Q_off_normalized = np.zeros((4, 4))
for i in range(4):
    total_off_diag = -Q[i, i]  # = Σ_{j≠i} Q[i,j]
    if total_off_diag > 0:
        for j in range(4):
            if i != j:
                Q_off_normalized[i, j] = Q[i, j] / total_off_diag
```

이 정규화는 Phase 2 Step 2.5에서 수행하지만,
Q 행렬과 함께 Phase 1의 산출물로 문서화해둔다.

### 결과물

대각 성분 $q_{ii} = -\sum_{j \neq i} q_{ij}$를 만족하는 생성 행렬 Q (4×4).
리그별 또는 전체. Phase 3에서 행렬 지수함수 $e^{Q \cdot \Delta t}$에 사용된다.

---

## Step 1.3: 프리매치 Prior 파라미터 a 학습 (Machine Learning)

### 목표

경기별 전력차를 반영한 기본 득점 강도(Base Intensity)의 **초기 추정치**를 제공한다.
이 값은 Step 1.4의 Joint Optimization에서 시작점으로 사용되며, 최종 a는 NLL이 결정한다.

### 피처 아키텍처 — 3-Tier 구조

Goalserve 풀 패키지로 가능한 피처를 3개 계층으로 구성한다:

#### Tier 1: 팀 레벨 롤링 스탯

**소스: Goalserve Live Game Stats — `stats.{team}`**

과거 5경기의 팀 스탯을 롤링 평균으로 집계:

| 피처 | Goalserve 필드 | 계산 |
|------|---------------|------|
| xG_per_90 | Live Game Stats xG 필드 | xG / 경기 수 |
| xGA_per_90 | 상대팀의 xG | 실점 위협 |
| shots_per_90 | `stats.shots.total` | 총 슈팅 빈도 |
| shots_on_target_per_90 | `stats.shots.ongoal` | 유효 슈팅 빈도 |
| shots_insidebox_ratio | `stats.shots.insidebox / stats.shots.total` | 박스 침투율 |
| possession_avg | `stats.possestiontime.total` | 점유율 |
| pass_accuracy | `stats.passes.accurate / stats.passes.total` | 패스 정확도 |
| corners_per_90 | `stats.corners.total` | 코너킥 빈도 |
| fouls_per_90 | `stats.fouls.total` | 파울 빈도 (공격성 proxy) |
| saves_per_90 | `stats.saves.total` | GK 세이브 빈도 |

**per-half 분리 피처 (선택적 확장):**

Goalserve는 `_h1`, `_h2` suffix로 전후반 분리 스탯을 제공한다:

| 피처 | 의미 |
|------|------|
| shots_h2_ratio | 후반 슈팅 비중 → 체력/전술 변화 proxy |
| possession_h1_vs_h2 | 전후반 점유율 차이 → 경기 운영 패턴 |

#### Tier 2: 선수 레벨 집계 피처

**소스: Goalserve Live Game Stats — `player_stats.{team}.player[]`**

오늘 선발 11명(Phase 2에서 확정)의 최근 5경기 스탯을 포지션별로 집계:

```python
def build_player_tier_features(starting_11_ids: List[str],
                                player_history: Dict) -> dict:
    """
    선발 11명의 과거 경기 스탯 → 팀 레벨 집계.
    player_history: {player_id: [최근 5경기의 player_stats]}
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
        
        # 포지션 그룹별 롤링 지표
        ratings = []
        goals_p90 = []
        key_passes_p90 = []
        tackles_p90 = []
        
        for pid in players_in_group:
            for game_stats in player_history[pid]:
                mp = float(game_stats.get("minutes_played") or 0)
                if mp < 10:
                    continue  # 너무 짧은 출전은 제외
                
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

**핵심 선수 집계 피처:**

| 피처 | 포지션 | 계산 | 의미 |
|------|--------|------|------|
| fw_avg_rating | FW | 롤링 평균 rating | 공격진 현재 폼 |
| fw_goals_p90 | FW | goals / minutes × 90 합산 | 공격진 득점 생산성 |
| mf_key_passes_p90 | MF | keyPasses / minutes × 90 합산 | 창의성 |
| mf_pass_accuracy | MF | passes_acc / passes 평균 | 빌드업 능력 |
| df_tackles_p90 | DF | tackles / minutes × 90 합산 | 수비 강도 |
| df_interceptions_p90 | DF | interceptions / minutes × 90 합산 | 수비 위치 선정 |
| gk_save_rate | GK | saves / (saves + goals_conceded) | GK 퍼포먼스 |
| team_avg_rating | 전원 | 출전시간 가중 평균 rating | 전체 팀 폼 |

> **minutes_played 주의:** Goalserve에서 벤치 미출전 선수는 `minutes_played`가 빈 문자열이다.
> 이런 경기는 롤링 평균에서 제외해야 한다. `mp < 10`인 교체 출전도 통계적으로 불안정하므로 제외.

#### Tier 3: 배당률 피처

**소스: Goalserve Pregame Odds — 20+ 북메이커**

```python
def build_odds_features(bookmakers: List[dict]) -> dict:
    """20+ 북메이커 배당 → 피처 벡터"""
    
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
    
    # Pinnacle이 없으면 시장 평균 사용
    if pinnacle_prob is None:
        pinnacle_prob = tuple(np.mean(all_probs, axis=0))
    
    return {
        "pinnacle_home_prob": pinnacle_prob[0],
        "pinnacle_draw_prob": pinnacle_prob[1],
        "pinnacle_away_prob": pinnacle_prob[2],
        "market_avg_home_prob": np.mean([p[0] for p in all_probs]),
        "market_avg_draw_prob": np.mean([p[1] for p in all_probs]),
        "bookmaker_odds_std": np.std([p[0] for p in all_probs]),
        # open/close가 있으면 추가
        # "odds_movement": close_home - open_home,
    }
```

| 피처 | 의미 |
|------|------|
| pinnacle_home/draw/away_prob | 가장 효율적인 시장의 내재 확률 |
| market_avg_home_prob | 20+ 북메이커 합의 확률 |
| bookmaker_odds_std | 시장 불확실성 (std 높으면 "어려운 경기") |
| odds_movement | 배당 움직임 (킥오프 전 정보 유입 방향) |

#### Tier 4: 컨텍스트 피처

| 피처 | 소스 | 계산 |
|------|------|------|
| home_away_flag | Fixtures | localteam/visitorteam 구분 |
| rest_days | Fixtures 날짜 차이 | 이전 경기로부터 휴식일 |
| h2h_goal_diff | Fixtures H2H | 최근 5 H2H 골차 평균 |

### 피처 선택

XGBoost의 내장 feature importance(gain 기준):

$$\text{Importance}(f) = \sum_{\text{splits on } f} \Delta \mathcal{L}_{\text{Poisson}}$$

누적 중요도 95%에 도달하는 상위 d'개 피처를 선택하고,
`feature_mask.json`으로 저장한다. Phase 2 추론 시 동일 마스크 적용.

> **PCA를 사용하지 않는 이유:** PCA는 선형 투영이므로 비선형 모델(XGBoost)과
> 불일치가 발생한다. XGBoost의 Poisson deviance 기반 중요도가 목적에 더 부합한다.

### 타겟(y)

해당 경기에서 각 팀의 **총 득점 수** (추가시간 득점 포함, VAR 취소골 제외).
홈팀과 어웨이팀에 대해 **별도 모델** 또는 **홈/어웨이 플래그를 포함한 단일 모델** 사용.

### 모델링

- XGBoost / LightGBM, 목적 함수: `count:poisson`
- 출력: 각 팀의 경기 전체 기대 득점 $\hat{\mu}_H, \hat{\mu}_A$

### a의 초기값 변환

$$a_H^{(init)} = \ln\!\left(\frac{\hat{\mu}_H}{T_m}\right), \quad a_A^{(init)} = \ln\!\left(\frac{\hat{\mu}_A}{T_m}\right)$$

"상수 강도 가정 하의 초기 추정". Step 1.4에서 b_i와 함께 보정된다.

### 결과물

- 학습된 XGBoost 가중치 파일 (`.xgb`)
- `feature_mask.json`
- 새로운 경기의 피처가 들어오면 $\hat{\mu}_H, \hat{\mu}_A$ 출력

### 피처 가용성에 따른 Fallback 전략

Live Game Stats의 과거 데이터 범위에 따라:

| 가용 범위 | Tier 2 (선수 레벨) 전략 |
|----------|----------------------|
| 5+ 시즌 | 전 기간 Tier 2 적용 |
| 2~4 시즌 | 최근 기간만 Tier 2, 나머지 Tier 1만 |
| 1시즌 이하 | Tier 2 비활성화, Tier 1 + Tier 3으로만 학습 |

Trial 기간에 `player_stats`의 소급 범위를 반드시 확인한다.

---

## Step 1.4: Joint NLL 최적화 (MMPP Calibration)

### 목표

시간 프로파일, 레드카드 패널티, 스코어차 효과, 경기별 기본 강도를 **동시에** 최적화한다.

### 순환 의존성 해소

a의 올바른 변환에는 b가 필요하다:

$$a = \ln\!\left(\frac{\hat{\mu} \cdot b}{e^{bT} - 1}\right) \quad \leftarrow \text{b가 필요 (순환 참조)}$$

**해법:** a를 고정 상수가 아닌 **학습 가능 파라미터**로 선언하고,
b, γ, δ와 함께 NLL을 최소화한다.
a에는 ML 예측치를 향한 정규화 항을 부여하여 과적합을 방지한다.

### 시간 기저함수 (Piecewise Basis)

$$\sum_{i=1}^{K} b_i \cdot B_i(t), \quad K = 6$$

| i | $B_i(t)$ | 커버 구간 |
|---|----------|----------|
| 1 | $\mathbb{1}_{[0, 15)}(t)$ | 전반 초반 |
| 2 | $\mathbb{1}_{[15, 30)}(t)$ | 전반 중반 |
| 3 | $\mathbb{1}_{[30, 45+\alpha_1)}(t)$ | 전반 종반 + 전반 추가시간 |
| 4 | $\mathbb{1}_{[HT_{end}, HT_{end}+15)}(t)$ | 후반 초반 |
| 5 | $\mathbb{1}_{[HT_{end}+15, HT_{end}+30)}(t)$ | 후반 중반 |
| 6 | $\mathbb{1}_{[HT_{end}+30, T_m)}(t)$ | 후반 종반 + 후반 추가시간 |

하프타임 구간은 어떤 $B_i$에도 포함되지 않으므로 자동으로 적분에서 제외된다.

> **$t_{eff}$ 변환 적용 시:** 기저함수는 단순히 $B_i = \mathbb{1}_{[15(i-1), 15i)}$로 정의 가능.

**per-half 스탯으로 b의 sanity check (Goalserve 고유 장점):**

Goalserve Live Game Stats는 shots, passes를 `_h1`, `_h2`로 분리 제공한다.
학습된 b[1..6]의 전후반 비중이 실제 슈팅 비중과 대략 일치하는지 확인 가능:

$$\frac{\exp(b_1) + \exp(b_2) + \exp(b_3)}{\sum_{i=1}^{6} \exp(b_i)} \approx \frac{\text{shots.total\_h1}}{\text{shots.total}} \quad \text{(리그 평균)}$$

### 레드카드 패널티 γ — 팀별 분리

홈팀과 어웨이팀이 **별도의 γ**를 사용한다.
레드카드는 한 팀에는 불이익, 상대팀에는 이익이므로 동일 γ를 공유할 수 없다.

**홈팀 γ^H:**

$$\gamma^H = [0,\; \gamma^H_1,\; \gamma^H_2,\; \gamma^H_1 + \gamma^H_2]$$

| 상태 | $\gamma^H$ | 물리적 의미 |
|------|-----------|------------|
| 0 (11v11) | 0 | 기준점 |
| 1 (홈 퇴장) | $\gamma^H_1 < 0$ | 홈팀 수적 열세 → 홈 득점력 **하락** |
| 2 (어웨이 퇴장) | $\gamma^H_2 > 0$ | 홈팀 수적 우세 → 홈 득점력 **상승** |
| 3 (양팀 퇴장) | $\gamma^H_1 + \gamma^H_2$ | 가산 합성 |

**어웨이팀 γ^A:**

$$\gamma^A = [0,\; \gamma^A_1,\; \gamma^A_2,\; \gamma^A_1 + \gamma^A_2]$$

| 상태 | $\gamma^A$ | 물리적 의미 |
|------|-----------|------------|
| 0 (11v11) | 0 | 기준점 |
| 1 (홈 퇴장) | $\gamma^A_1 > 0$ | 어웨이 수적 우세 → 어웨이 득점력 **상승** |
| 2 (어웨이 퇴장) | $\gamma^A_2 < 0$ | 어웨이 수적 열세 → 어웨이 득점력 **하락** |
| 3 (양팀 퇴장) | $\gamma^A_1 + \gamma^A_2$ | 가산 합성 |

**선택적 대칭 제약:**

$$\gamma^A_1 = -\gamma^H_2, \quad \gamma^A_2 = -\gamma^H_1$$

자유 파라미터: 4개 (비대칭) 또는 2개 (대칭). Step 1.5에서 실증 비교.

### Score-Dependent Intensity δ(ΔS)

| ΔS | 홈팀 $\delta_H$ | 어웨이팀 $\delta_A$ | 해석 |
|----|----------------|-------------------|------|
| ≤ -2 | $\delta_H^{(-2)}$ | $\delta_A^{(-2)}$ | 홈 크게 뒤짐 |
| -1 | $\delta_H^{(-1)}$ | $\delta_A^{(-1)}$ | 홈 약간 뒤짐 |
| 0 | 0 (고정) | 0 (고정) | **기준점** |
| +1 | $\delta_H^{(+1)}$ | $\delta_A^{(+1)}$ | 홈 약간 앞섬 |
| ≥ +2 | $\delta_H^{(+2)}$ | $\delta_A^{(+2)}$ | 홈 크게 앞섬 |

- 기준점 고정: δ(0) = 0 → 식별성 확보
- |ΔS| ≥ 3 병합: |ΔS| = 2와 동일 δ 사용 (데이터 부족)
- 자유 파라미터: 홈 4개 + 어웨이 4개 = 8개 (대칭 제약 시 4개)

### 구간별 적분 (Closed-Form) — 홈/어웨이 별도

구간 k에서 $(X_k, \Delta S_k)$가 일정하고, 기저함수 $B_{i_k}$에 속할 때:

$$\mu^H_k = \exp\!\left(a^m_H + b_{i_k} + \gamma^H_{X_k} + \delta_H(\Delta S_k)\right) \cdot (t_k - t_{k-1})$$

$$\mu^A_k = \exp\!\left(a^m_A + b_{i_k} + \gamma^A_{X_k} + \delta_A(\Delta S_k)\right) \cdot (t_k - t_{k-1})$$

### 점 이벤트 기여 (골 시각) — 홈/어웨이 분리

홈팀 골: $\ln \lambda_H(t_g) = a^m_H + b_{i(t_g)} + \gamma^H_{X(t_g)} + \delta_H(\Delta S_{before,g})$

어웨이팀 골: $\ln \lambda_A(t_g) = a^m_A + b_{i(t_g)} + \gamma^A_{X(t_g)} + \delta_A(\Delta S_{before,g})$

> **자책골:** 점 이벤트 항에서 **제외** (Step 1.1의 자책골 처리 방침).
> 구간 적분 항에는 ΔS 반영으로 정상 포함.

### Loss 함수 (최종 NLL)

$$\mathcal{L} = \underbrace{-\sum_{m=1}^{M}\Bigg[\sum_{g \in \text{HomeGoals}_m} \ln \lambda_H(t_g) + \sum_{g \in \text{AwayGoals}_m} \ln \lambda_A(t_g) - \sum_{k \in \text{Intervals}_m} \left(\mu^H_k + \mu^A_k\right)\Bigg]}_{\text{Negative Log-Likelihood}}$$

$$+ \underbrace{\frac{1}{2\sigma_a^2}\sum_{m=1}^M \left[(a^m_H - a^{m,(init)}_H)^2 + (a^m_A - a^{m,(init)}_A)^2\right]}_{\text{ML Prior 정규화}}$$

$$+ \underbrace{\lambda_{reg}\left(\|\mathbf{b}\|^2 + \|\boldsymbol{\gamma}^H\|^2 + \|\boldsymbol{\gamma}^A\|^2 + \|\boldsymbol{\delta}_H\|^2 + \|\boldsymbol{\delta}_A\|^2\right)}_{\text{L2 정규화}}$$

> **HomeGoals, AwayGoals에서 자책골 제외.** `owngoal=True`인 골은 합산에서 빠진다.
> **VAR 취소골 제외.** `var_cancelled=True`인 골은 이미 Step 1.1에서 필터링됨.

### 학습 가능 파라미터 (PyTorch `nn.Parameter`)

| 파라미터 | 차원 | 초기값 | 비고 |
|---------|------|--------|------|
| $a^m_H$ | M × 1 | $\ln(\hat{\mu}^m_H / T_m)$ | 경기별 홈 기본 강도 |
| $a^m_A$ | M × 1 | $\ln(\hat{\mu}^m_A / T_m)$ | 경기별 어웨이 기본 강도 |
| **b** | 6 × 1 | **0** | 구간별 시간 프로파일 |
| $\gamma^H_1, \gamma^H_2$ | 2 scalars | 0, 0 | 홈팀 퇴장 패널티 |
| $\gamma^A_1, \gamma^A_2$ | 2 scalars | 0, 0 | 어웨이팀 퇴장 패널티 |
| $\boldsymbol{\delta}_H$ | 4 × 1 | **0** | 홈팀 스코어차 효과 |
| $\boldsymbol{\delta}_A$ | 4 × 1 | **0** | 어웨이팀 스코어차 효과 |

총 자유 파라미터: $2M + 6 + 4 + 8 = 2M + 18$
(γ 대칭 시 $2M + 16$, δ 대칭 추가 시 $2M + 12$)

### 파라미터 클램핑

| 파라미터 | 허용 범위 | 물리적 근거 |
|---------|----------|------------|
| $b_i$ | [-0.5, 0.5] | 구간 간 강도 변화가 ×1.65를 초과하면 비현실적 |
| $\gamma^H_1$ | [-1.5, 0] | 홈 퇴장 → 홈 득점력 하락 |
| $\gamma^H_2$ | [0, 1.5] | 어웨이 퇴장 → 홈 득점력 상승 |
| $\gamma^A_1$ | [0, 1.5] | 홈 퇴장 → 어웨이 득점력 상승 |
| $\gamma^A_2$ | [-1.5, 0] | 어웨이 퇴장 → 어웨이 득점력 하락 |
| $\delta_H^{(-2)}, \delta_H^{(-1)}$ | [-0.5, 1.0] | 홈 뒤지면 공격 ↑ |
| $\delta_H^{(+1)}, \delta_H^{(+2)}$ | [-1.0, 0.5] | 홈 앞서면 수비 전환 ↓ |
| $\delta_A^{(-2)}, \delta_A^{(-1)}$ | [-1.0, 0.5] | 어웨이 앞서면 수비 전환 ↓ |
| $\delta_A^{(+1)}, \delta_A^{(+2)}$ | [-0.5, 1.0] | 어웨이 뒤지면 공격 ↑ |

### 최적화 전략

**1. Multi-Start:**
NLL이 비볼록(non-convex)이므로, b, γ, δ의 초기값을
5~10개 랜덤 시드에서 출발하여 최적의 로컬 미니멈을 선택한다.

**2. 2단계 옵티마이저:**
Adam (lr=1e-3, 1000 epochs) → L-BFGS (fine-tuning).

**3. 수치 안정성:**
구간별 기저함수 모델에서는 b → 0 특이점이 원천적으로 발생하지 않는다.

### 결과물

- 시간 구간별 득점 강도 프로파일 $\mathbf{b} = [b_1, \ldots, b_6]$
- 홈팀 퇴장 패널티 $\gamma^H_1, \gamma^H_2$ (+ $\gamma^H_3 = \gamma^H_1 + \gamma^H_2$)
- 어웨이팀 퇴장 패널티 $\gamma^A_1, \gamma^A_2$ (+ $\gamma^A_3 = \gamma^A_1 + \gamma^A_2$)
- 스코어차 효과 $\boldsymbol{\delta}_H, \boldsymbol{\delta}_A$
- 보정된 경기별 기본 강도 $\{a^m_H, a^m_A\}$

---

## Step 1.5: 시계열 교차검증 및 모델 진단 (Validation)

### 목표

과적합을 검출하고 모델의 확률 예측 정확도를 정량화한다.
**이 단계를 통과하지 않으면 라이브에 투입하지 않는다.**

### Walk-Forward Validation

| Fold | 학습 기간 | 검증 기간 |
|------|----------|----------|
| 1 | 시즌 1~3 | 시즌 4 |
| 2 | 시즌 1~4 | 시즌 5 |
| 3 | 시즌 2~5 | 시즌 6 |

각 Fold에서 Step 1.3(ML)과 Step 1.4(NLL)를 학습 기간 데이터로만 수행하고,
검증 기간에서 다음 지표를 측정한다.

### 핵심 진단 지표

**1. Calibration Plot (Reliability Diagram):**

모델이 "P = 0.6"이라 예측한 이벤트가 실제 60% 빈도로 발생하는지 시각화.

**2. Brier Score — Pinnacle 기준선:**

Goalserve Pregame Odds의 **Pinnacle close line**으로 시장 기준선을 정밀하게 설정:

$$BS_{model} = \frac{1}{N}\sum_n (P_{model,n} - O_n)^2$$

$$BS_{pinnacle} = \frac{1}{N}\sum_n (P_{pinnacle\_close,n} - O_n)^2$$

$$\Delta BS = BS_{model} - BS_{pinnacle}$$

$\Delta BS < 0$이면 모델이 세계에서 가장 효율적인 시장(Pinnacle)보다 나은 것.

> **Goalserve 고유 장점:** 20+ 북메이커의 historical close odds가 제공되므로,
> Pinnacle뿐 아니라 bet365, Marathonbet 등 다양한 기준선과 비교 가능.

**3. Log Loss (검증 세트 NLL):**

$$\text{Log Loss} = -\frac{1}{N}\sum_{n=1}^N [O_n \ln P_n + (1-O_n)\ln(1-P_n)]$$

**4. 시뮬레이션 P&L:**

검증 기간의 Goalserve Pregame Odds 과거 데이터가 있으므로,
Kalshi 호가를 Pinnacle 배당으로 대리하여 Phase 4의 Kelly 로직까지 시뮬레이션.

**5. 다중 마켓 교차 검증 (Goalserve 고유):**

Goalserve Pregame Odds는 50+ 마켓을 제공하므로, 모델의 μ_H, μ_A로부터
**여러 마켓의 확률을 동시에 검증**할 수 있다:

| 마켓 | 모델에서 도출 | 시장에서 도출 | 비교 |
|------|-------------|-------------|------|
| Match Winner | Poisson(μ_H) vs Poisson(μ_A) | Pregame Odds 1X2 | BS 비교 |
| Over/Under 2.5 | 1 - CDF(2, μ_H + μ_A) | Pregame Odds O/U | BS 비교 |
| Both Teams to Score | 복합 Poisson | Pregame Odds BTTS | BS 비교 |

모델이 1X2에서는 좋은데 O/U에서 나쁘면 → μ의 합은 맞지만 비율이 잘못됐을 가능성.
모든 마켓에서 동시에 좋아야 μ_H, μ_A가 올바르게 추정된 것이다.

### γ 부호 검증

| 기대 부호 | 검증 |
|----------|------|
| $\gamma^H_1 < 0$ | 홈 퇴장 → 홈 득점력 하락 |
| $\gamma^H_2 > 0$ | 어웨이 퇴장 → 홈 득점력 상승 |
| $\gamma^A_1 > 0$ | 홈 퇴장 → 어웨이 득점력 상승 |
| $\gamma^A_2 < 0$ | 어웨이 퇴장 → 어웨이 득점력 하락 |

**γ 대칭 vs 비대칭 비교:**

- 대칭 모델: $\gamma^A_1 = -\gamma^H_2$, $\gamma^A_2 = -\gamma^H_1$ (2 파라미터)
- 비대칭 모델: 독립 4 파라미터

검증 세트 Log Loss 비교 → 비대칭 정당화 여부 판단.

### δ 부호 검증

| 기대 부호 | 검증 |
|----------|------|
| $\delta_H^{(-1)} > 0$ | 뒤지는 홈팀 공격 강화 |
| $\delta_H^{(+1)} < 0$ | 앞서는 홈팀 수비 전환 |
| $\delta_A^{(-1)} < 0$ | 앞서는 어웨이팀 수비 전환 |
| $\delta_A^{(+1)} > 0$ | 뒤지는 어웨이팀 공격 강화 |

**δ = 0 기각 검증 (Likelihood Ratio Test):**

$$LR = -2(\mathcal{L}_{\delta=0} - \mathcal{L}_{\delta \neq 0}) \sim \chi^2(df)$$

p < 0.05이면 δ 도입 정당화. 기각 실패 시 δ 없는 모델 유지.

**δ 대칭 vs 비대칭 비교:**

- 대칭: $\delta_A(\Delta S) = \delta_H(-\Delta S)$ (4 파라미터)
- 비대칭: 독립 8 파라미터

### b 검증 — per-half 스탯 교차 확인 (Goalserve 고유)

학습된 b[1..6]의 전후반 비중을 Goalserve의 실제 전후반 슈팅 비중과 비교:

```python
def validate_b_with_half_stats(b, stats_db):
    """
    Goalserve Live Game Stats의 shots.total_h1, shots.total_h2로
    학습된 b의 전후반 비중을 교차 검증
    """
    # 모델의 전반 가중치
    model_h1_weight = sum(np.exp(b[i]) for i in range(3))
    model_h2_weight = sum(np.exp(b[i]) for i in range(3, 6))
    model_h1_ratio = model_h1_weight / (model_h1_weight + model_h2_weight)
    
    # 실제 전반 슈팅 비중 (리그 평균)
    actual_h1_ratio = stats_db["shots_h1_total"] / stats_db["shots_total"]
    
    discrepancy = abs(model_h1_ratio - actual_h1_ratio)
    if discrepancy > 0.10:
        log.warning(f"b half-ratio mismatch: model={model_h1_ratio:.2f}, "
                    f"actual={actual_h1_ratio:.2f}")
```

### 통과 기준 (Go/No-Go)

| 기준 | 임계값 |
|------|--------|
| Calibration plot | 대각선 ±5% 이내 |
| Brier Score | $\Delta BS < 0$ (Pinnacle 대비 개선) |
| 다중 마켓 BS | 1X2, O/U, BTTS 모두 시장 대비 개선 |
| 시뮬레이션 Max Drawdown | 자본의 20% 이하 |
| 전체 Fold | 3개 Fold 모두에서 양의 시뮬레이션 수익 |
| γ 부호 | 4개 모두 축구 직관과 부합 |
| δ 부호 | 축구 직관과 부합 |
| b 전후반 비중 | 실제 슈팅 비중과 ±10% 이내 |

### 결과물

모든 기준을 통과한 최종 파라미터 세트를 **프로덕션 파라미터**로 확정하고 Phase 2로 전달한다.

---

## Phase 1 → Phase 2 핸드오프

| 파라미터 | 출처 | 용도 |
|---------|------|------|
| XGBoost weights + `feature_mask.json` | Step 1.3 | 신규 경기의 $\hat{\mu}_H, \hat{\mu}_A$ 예측 |
| $\mathbf{b} = [b_1, \ldots, b_6]$ | Step 1.4 | 시간 구간별 득점 강도 프로파일 |
| $\gamma^H_1, \gamma^H_2$ | Step 1.4 | 퇴장 시 홈팀 강도 점프 |
| $\gamma^A_1, \gamma^A_2$ | Step 1.4 | 퇴장 시 어웨이팀 강도 점프 |
| $\boldsymbol{\delta}_H, \boldsymbol{\delta}_A$ | Step 1.4 | 스코어차에 따른 강도 보정 |
| Q (4×4 행렬) | Step 1.2 | 미래 퇴장 확률 (행렬 지수함수용) |
| $\mathbb{E}[\alpha_1], \mathbb{E}[\alpha_2]$ | Step 1.1 | Phase 2의 $T_{exp}$ 계산 |
| δ 유의성 플래그 (`DELTA_SIGNIFICANT`) | Step 1.5 LRT | Phase 3 해석적/MC 모드 결정 |
| Pinnacle BS 기준선 | Step 1.5 | Phase 4 사후 분석의 시장 기준 |

> **δ와 Phase 2의 관계:** 킥오프 시점에 ΔS = 0이므로 δ(0) = 0.
> Phase 2의 a 역산 공식에 δ가 영향을 주지 않는다.
> δ는 Phase 3에서 골이 발생한 후부터 활성화된다.

---

## Phase 1 파이프라인 요약

```
[Goalserve Full Package: 5+ 시즌, 500+ 리그]
              │
              ▼
┌───────────────────────────────────────────────────────────────┐
│  Step 1.1: 구간 분할 (Data Engineering)                         │
│  • Fixtures/Results → 골(VAR 필터링) + 레드카드 이벤트           │
│  • addedTime_period1/2 → 경기별 T_m                            │
│  • var_cancelled=True → 제외, owngoal → 점 이벤트 제외          │
│  • 각 구간에 (X, ΔS) 태그, 골에 ΔS_before + 득점 팀 기록       │
│  Output: intervals[], home_goal_events[], away_goal_events[]   │
└──────────────────┬────────────────────────────────────────────┘
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
┌──────────────┐    ┌──────────────────────────────────────────┐
│  Step 1.2:   │    │  Step 1.3: ML Prior (XGBoost)            │
│  Q 행렬 추정  │    │  • Tier 1: 팀 롤링 스탯 (xG 포함)        │
│  • Fixtures   │    │  • Tier 2: 선수 레벨 집계 (rating 등)    │
│    redcards   │    │  • Tier 3: 배당률 (20+ 북메이커)          │
│  • 경험적     │    │  • Tier 4: 컨텍스트 (H/A, 휴식일, H2H)   │
│    전이율     │    │  • Feature Importance 피처 선택           │
│  • γ^H/A     │    │  • Poisson 회귀 → μ̂_H, μ̂_A            │
│    가산 합성  │    │  Output: â_H^(init), â_A^(init), .xgb    │
│  • 리그 층화  │    └──────────────┬───────────────────────────┘
└──────┬───────┘                   │
       │                           │
       └───────────┬───────────────┘
                   ▼
┌───────────────────────────────────────────────────────────────┐
│  Step 1.4: Joint NLL 최적화 (PyTorch)                           │
│  • a^m_H, a^m_A, b[1..6], γ^H₁, γ^H₂, γ^A₁, γ^A₂,          │
│    δ_H[4], δ_A[4] 동시 학습                                    │
│  • 홈/어웨이 골 분리 NLL                                        │
│  • 자책골 점 이벤트 제외                                        │
│  • Multi-start + L2 정규화 + 클램핑                             │
│  Output: b[], γ^H, γ^A, δ_H[], δ_A[], {a^m_H, a^m_A}         │
└──────────────────┬────────────────────────────────────────────┘
                   ▼
┌───────────────────────────────────────────────────────────────┐
│  Step 1.5: 시계열 교차검증 (Validation)                          │
│  • Walk-Forward CV (temporal leakage 방지)                     │
│  • Brier Score vs Pinnacle close line (Goalserve Odds)        │
│  • 다중 마켓 교차 검증 (1X2 + O/U + BTTS)                      │
│  • b 전후반 비중 vs 실제 슈팅 비중 (Goalserve per-half stats)   │
│  • γ 부호 검증 (4개), δ 부호 검증, LRT                         │
│  • γ/δ 대칭 vs 비대칭 비교                                      │
│  • 시뮬레이션 P&L + Max Drawdown                               │
│  • Go/No-Go 판정                                              │
│  Output: Production Parameters + DELTA_SIGNIFICANT flag        │
└───────────────────────────────────────────────────────────────┘
```
