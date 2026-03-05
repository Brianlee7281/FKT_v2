# Phase 2: Pre-Match Initialization — Goalserve Full Package

## 개요

킥오프 전, Phase 1에서 학습한 파라미터를 오늘 경기의 현실에 맞게 초기화하여
Live Trading Engine의 **초기 조건(Initial Condition)**을 설정하는 단계.

Phase 1이 "과거의 지혜"를 수식화했다면,
Phase 2는 그 지혜를 "오늘의 라인업과 컨디션"에 미세 조정(Calibration)하는 과정이다.

킥오프 1시간 전, 선발 라인업이 발표되는 시점부터 경기 시작 직전까지
시스템 내부에서 어떤 수학적 연산과 데이터 파이프라인이 흘러가는지
5개의 Step으로 분해한다.

### 데이터 소스 일원화

Phase 1과 동일하게 **Goalserve 단일 소스**로 통일한다.
Phase 1 학습 데이터와 Phase 2 추론 데이터의 스키마가 100% 동일하므로,
피처 매핑 오류가 원천적으로 차단된다.

| Goalserve 패키지 | Phase 2 역할 | 핵심 데이터 |
|-----------------|-------------|------------|
| **Live Game Stats** | 라인업 + 포메이션 (킥오프 60분 전) | `teams.{team}.player[]`, `formation` |
| **Live Game Stats** (과거) | 선수별 롤링 스탯 | `player_stats.{team}.player[]` |
| **Fixtures/Results** (과거) | 팀 롤링 스탯, 휴식일, H2H | `stats.{team}`, 경기 날짜 |
| **Pregame Odds** | 배당률 피처 + Sanity Check | 20+ 북메이커, 50+ 마켓 |
| **Live Score** | 라이브 이벤트 수신 준비 | REST 폴링 3초 (Phase 3에서 소비) |
| **Live Odds** | ob_freeze 센서 + 1차 이벤트 감지 준비 | WebSocket PUSH <1초 (Phase 3에서 소비) |

---

## Input Data

**Phase 1 프로덕션 파라미터:**

| 파라미터 | 출처 |
|---------|------|
| XGBoost weights + `feature_mask.json` | Step 1.3 |
| $\mathbf{b} = [b_1, \ldots, b_6]$ | Step 1.4 |
| $\gamma^H_1, \gamma^H_2$ (홈팀 퇴장 패널티) | Step 1.4 |
| $\gamma^A_1, \gamma^A_2$ (어웨이팀 퇴장 패널티) | Step 1.4 |
| $\boldsymbol{\delta}_H, \boldsymbol{\delta}_A$ (스코어차 효과) | Step 1.4 |
| Q (4×4 행렬) | Step 1.2 |
| $\mathbb{E}[\alpha_1], \mathbb{E}[\alpha_2]$ (평균 추가시간) | Step 1.1 |
| `DELTA_SIGNIFICANT` (δ 유의성 플래그) | Step 1.5 LRT |

**Goalserve 실시간 엔드포인트:**

```
# 라인업 + 선수 스탯 (킥오프 60분 전)
GET /getfeed/{api_key}/soccerstats/match/{match_id}?json=1

# 당일 배당률
GET /getfeed/{api_key}/soccernew/{league_id}?json=1

# 라이브 스코어 (Phase 3용, 여기서 연결 검증만)
GET /getfeed/{api_key}/soccerlive/home?json=1

# 라이브 배당 (Phase 3용, 여기서 연결 검증만)
WebSocket wss://goalserve.com/... (Live Odds PUSH)
```

---

## Step 2.1: 경기 전 컨텍스트 데이터 수집 (Data Ingestion)

### 타이밍

킥오프 약 **60분 전** — Goalserve Live Game Stats가 라인업을 이 시점에 제공.

### 2.1.1: 라인업 + 포메이션 수집

**Goalserve Live Game Stats → `teams.{team}`:**

```json
{
  "formation": "4-3-3",
  "player": [
    {
      "formation_pos": "1",
      "id": "102587",
      "name": "Emiliano Martínez",
      "number": "23",
      "pos": "G"
    },
    {
      "formation_pos": "9",
      "id": "119",
      "name": "Lionel Messi",
      "number": "10",
      "pos": "F"
    }
  ]
}
```

| 추출 데이터 | 필드 | 용도 |
|------------|------|------|
| 선발 11명 ID | `player[].id` | 선수별 롤링 스탯 조회 키 |
| 포메이션 | `formation` ("4-3-3") | 포메이션 피처 (선택적) |
| 포지션 | `player[].pos` (G/D/M/F) | 포지션 가중 집계 |
| 포메이션 내 위치 | `player[].formation_pos` | 세부 역할 추론 (CB vs FB 등) |

**벤치 — `substitutes.{team}.player[]`:**

```json
{
  "id": "404462",
  "name": "Lautaro Martínez",
  "number": "22",
  "pos": "F"
}
```

벤치 정보는 직접 모델에 쓰지 않지만, **킥오프 전 라인업 변경 감지**에 필요하다.
선발↔벤치 교체가 발생하면 Step 2.1을 재실행해야 한다.

> **ID 일원화의 핵심 이점:** 선수 ID `119`(Messi)가 Phase 1 학습 데이터의 `player_stats`와
> Phase 2 추론의 `teams` 라인업에서 동일하다. 별도 ID 매핑 테이블이 불필요하다.

### 2.1.2: 선발 선수별 롤링 스탯 계산

Phase 1에서 DB에 적재해둔 과거 경기의 `player_stats`에서 **직접** 롤링 평균을 계산한다.
별도 선수 DB가 불필요하다.

**데이터 흐름:**

```
오늘 선발 11명의 player_id 확보 (Step 2.1.1)
        │
        ▼
각 선수의 최근 5경기 player_stats 조회
(Phase 1에서 Goalserve Live Game Stats 과거 데이터를 DB에 적재해둔 것)
        │
        ▼
선수별 per-90 지표 계산
        │
        ▼
포지션별 가중 집계 → 팀 레벨 피처 벡터
```

**선수별 per-90 지표:**

| 지표 | 계산 | Goalserve 필드 |
|------|------|---------------|
| goals_per_90 | goals / minutes_played × 90 | `goals`, `minutes_played` |
| shots_on_target_per_90 | shots_on_goal / minutes_played × 90 | `shots_on_goal` |
| key_passes_per_90 | keyPasses / minutes_played × 90 | `keyPasses` |
| pass_accuracy | passes_acc / passes | `passes_acc`, `passes` |
| dribble_success_rate | dribbleSucc / dribbleAttempts | `dribbleSucc`, `dribbleAttempts` |
| tackles_per_90 | tackles / minutes_played × 90 | `tackles` |
| interceptions_per_90 | interceptions / minutes_played × 90 | `interceptions` |
| rating_avg | rating 롤링 평균 | `rating` |

> **minutes_played 주의:** Goalserve에서 벤치 미출전 선수는 `minutes_played`가 빈 문자열이다.
> 이런 경기는 롤링 평균에서 제외한다. `minutes_played < 10`인 짧은 교체 출전도 제외.

**결측값 처리:**

```python
def safe_per90(stat_value: str, minutes: str) -> Optional[float]:
    """Goalserve 빈 문자열 → None 처리"""
    mp = float(minutes) if minutes else 0
    val = float(stat_value) if stat_value else 0
    if mp < 10:
        return None  # 통계적으로 불안정 → 롤링에서 제외
    return val / mp * 90
```

### 2.1.3: 포지션별 팀 레벨 집계

선발 11명의 선수 스탯을 포지션 그룹별로 집계:

```python
def aggregate_team_features(starting_11_stats: List[PlayerRolling]) -> dict:
    """
    선발 11명의 롤링 스탯 → 팀 레벨 피처 벡터.
    Phase 1 Step 1.3의 Tier 2 피처와 동일한 구조.
    """
    forwards = [p for p in starting_11_stats if p.pos == "F"]
    midfielders = [p for p in starting_11_stats if p.pos == "M"]
    defenders = [p for p in starting_11_stats if p.pos == "D"]
    goalkeeper = [p for p in starting_11_stats if p.pos == "G"]

    return {
        # 공격 피처 (FW)
        "fw_avg_rating": safe_mean([p.rating_avg for p in forwards]),
        "fw_goals_p90": safe_sum([p.goals_per_90 for p in forwards]),
        "fw_shots_on_target_p90": safe_sum([p.shots_on_target_per_90 for p in forwards]),

        # 창의성 피처 (MF)
        "mf_avg_rating": safe_mean([p.rating_avg for p in midfielders]),
        "mf_key_passes_p90": safe_sum([p.key_passes_per_90 for p in midfielders]),
        "mf_pass_accuracy": safe_mean([p.pass_accuracy for p in midfielders]),

        # 수비 피처 (DF)
        "df_avg_rating": safe_mean([p.rating_avg for p in defenders]),
        "df_tackles_p90": safe_sum([p.tackles_per_90 for p in defenders]),
        "df_interceptions_p90": safe_sum([p.interceptions_per_90 for p in defenders]),

        # GK 피처
        "gk_rating": goalkeeper[0].rating_avg if goalkeeper else None,
        "gk_save_rate": goalkeeper[0].save_rate if goalkeeper else None,

        # 전체 피처 (출전시간 가중)
        "team_avg_rating": weighted_mean(
            [p.rating_avg for p in starting_11_stats],
            weights=[p.minutes_played_avg for p in starting_11_stats]
        ),
    }
```

> **formation_pos 활용 — 향후 확장:**
> Goalserve의 `formation_pos`로 CB(3,4) vs FB(2,5) 구분이 가능하지만,
> Phase 1 학습에서도 동일 방식으로 집계해야 일관성이 유지된다.
> 초기에는 G/D/M/F 4분류로 시작하고, 시스템 안정화 후 세부 포지션으로 확장한다.

### 2.1.4: 팀 레벨 롤링 스탯

선수 레벨과 별도로, **팀 차원의 롤링 지표**를 Phase 1에서 DB에 적재해둔
과거 `stats.{team}`에서 계산한다:

| 피처 | Goalserve 필드 | 롤링 |
|------|---------------|------|
| xG_per_90 | Live Game Stats xG 필드 | 5경기 |
| xGA_per_90 | 상대팀 xG | 5경기 |
| possession_avg | `possestiontime.total` | 5경기 |
| shots_per_90 | `shots.total` | 5경기 |
| shots_insidebox_ratio | `shots.insidebox / shots.total` | 5경기 |
| pass_accuracy | `passes.accurate / passes.total` | 5경기 |
| corners_per_90 | `corners.total` | 5경기 |
| fouls_per_90 | `fouls.total` | 5경기 |

### 2.1.5: 배당률 수집

**Goalserve Pregame Odds — 20+ 북메이커, 50+ 마켓:**

```python
def extract_odds_features(bookmakers: List[dict]) -> dict:
    """
    20+ 북메이커 배당 → 피처 벡터 + Sanity Check 기준값.
    Phase 1 Step 1.3 Tier 3과 동일한 구조.
    """
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

    if pinnacle_prob is None:
        pinnacle_prob = tuple(np.mean(all_probs, axis=0))

    return {
        # 피처용 (Step 2.2 → XGBoost 입력)
        "pinnacle_home_prob": pinnacle_prob[0],
        "pinnacle_draw_prob": pinnacle_prob[1],
        "pinnacle_away_prob": pinnacle_prob[2],
        "market_avg_home_prob": np.mean([p[0] for p in all_probs]),
        "market_avg_draw_prob": np.mean([p[1] for p in all_probs]),
        "bookmaker_odds_std": np.std([p[0] for p in all_probs]),

        # Sanity Check용 (Step 2.4)
        "_pinnacle_raw": pinnacle_prob,
        "_market_avg_raw": tuple(np.mean(all_probs, axis=0)),
        "_all_bookmakers": bookmakers,  # O/U 교차 검증용
    }
```

### 2.1.6: 컨텍스트 피처

| 피처 | 소스 | 계산 |
|------|------|------|
| home_away_flag | Fixtures 메타 | localteam = 1, visitorteam = 0 |
| rest_days | Fixtures 날짜 차이 | 각 팀의 이전 경기로부터 일수 |
| h2h_goal_diff | Fixtures H2H | 최근 5 H2H 경기의 골차 평균 |

### Step 2.1 결과물

```python
@dataclass
class PreMatchData:
    # 라인업
    home_starting_11: List[str]     # Goalserve player IDs
    away_starting_11: List[str]
    home_formation: str             # "4-3-3"
    away_formation: str

    # Tier 2: 선수 집계 피처
    home_player_agg: dict           # aggregate_team_features() 출력
    away_player_agg: dict

    # Tier 1: 팀 롤링 스탯
    home_team_rolling: dict         # 과거 stats.{team} 기반
    away_team_rolling: dict

    # Tier 3: 배당률 피처
    odds_features: dict             # extract_odds_features() 출력

    # Tier 4: 컨텍스트
    home_rest_days: int
    away_rest_days: int
    h2h_goal_diff: float

    # 메타
    match_id: str                   # Goalserve match ID (전 Phase 통일)
    kickoff_time: str
```

---

## Step 2.2: 피처 선택 (Feature Selection)

### 목표

고차원 원시 피처에서 Phase 1에서 선정한 유효 피처만 추출하여 노이즈를 제거한다.

### Feature Mask 적용

Phase 1 Step 1.3에서 저장한 `feature_mask.json`을 적용한다.
Phase 1과 Phase 2가 **동일한 Goalserve 스키마**를 사용하므로
별도 피처 이름 매핑 로직이 불필요하다.

```python
def apply_feature_mask(pre_match: PreMatchData,
                       feature_mask: List[str],
                       median_values: Dict[str, float]) -> np.ndarray:
    """
    Phase 1의 feature_mask.json에 명시된 피처만 추출.
    결측값은 Phase 1 학습 데이터의 median으로 대체.

    피처 이름이 Phase 1과 동일한 Goalserve 스키마이므로
    수동 매핑 레이어가 불필요 → silent bug 차단.
    """
    # 전체 피처 벡터 구성
    full_vec = {}

    # Tier 1: 팀 롤링 (접두어로 home_/away_ 구분)
    for prefix, rolling in [("home_", pre_match.home_team_rolling),
                            ("away_", pre_match.away_team_rolling)]:
        for k, v in rolling.items():
            full_vec[prefix + k] = v

    # Tier 2: 선수 집계
    for prefix, agg in [("home_", pre_match.home_player_agg),
                        ("away_", pre_match.away_player_agg)]:
        for k, v in agg.items():
            full_vec[prefix + k] = v

    # Tier 3: 배당률 (팀 구분 없음)
    for k, v in pre_match.odds_features.items():
        if not k.startswith("_"):  # internal 필드 제외
            full_vec[k] = v

    # Tier 4: 컨텍스트
    full_vec["home_away_flag"] = 1  # 항상 홈 관점
    full_vec["home_rest_days"] = pre_match.home_rest_days
    full_vec["away_rest_days"] = pre_match.away_rest_days
    full_vec["h2h_goal_diff"] = pre_match.h2h_goal_diff

    # 마스크 적용
    selected = []
    for feat_name in feature_mask:
        val = full_vec.get(feat_name)
        if val is not None and not np.isnan(val):
            selected.append(val)
        else:
            selected.append(median_values[feat_name])  # 결측 대체
    
    return np.array(selected)
```

### 결과물

$$X_{match} \in \mathbb{R}^{d'}$$

Phase 1과 동일 차원, 동일 피처 순서의 피처 벡터.

---

## Step 2.3: 기본 득점 강도 파라미터 a의 역산 (Prior Inference)

### 목표

ML 모델의 예측값(기대 득점)을 Live Engine의 강도 함수 파라미터 a로 변환한다.

### ML 추론 (Inference)

Step 2.2의 $X_{match}$를 Phase 1에서 학습한 XGBoost Poisson 모델에 입력:

```python
import xgboost as xgb

def predict_expected_goals(X_match: np.ndarray, model_path: str) -> Tuple[float, float]:
    """
    XGBoost Poisson 모델로 홈/어웨이 기대 득점 예측.
    홈/어웨이 별도 모델이면 각각 호출,
    단일 모델이면 홈/어웨이 플래그로 구분.
    """
    model = xgb.Booster()
    model.load_model(model_path)
    
    dmat = xgb.DMatrix(X_match.reshape(1, -1))
    mu_hat = model.predict(dmat)[0]  # Poisson 기대값
    
    return mu_hat  # μ̂_H 또는 μ̂_A
```

- $\hat{\mu}_H$: 홈팀의 경기 전체 기대 득점
- $\hat{\mu}_A$: 어웨이팀의 경기 전체 기대 득점

### 수학적 역산 — Piecewise Basis 버전

킥오프 시점에서 X = 0, ΔS = 0이므로:

$$\lambda_H(t \mid X=0, \Delta S=0) = \exp\!\left(a_H + b_{i(t)}\right)$$

($\gamma^H_0 = 0$, $\delta_H(0) = 0$이므로 소거)

경기 전체의 기대 득점:

$$\hat{\mu}_H = \exp(a_H) \sum_{i=1}^{K} \exp(b_i) \cdot \Delta t_i = \exp(a_H) \cdot C_{time}$$

$$C_{time} \equiv \sum_{i=1}^{K} \exp(b_i) \cdot \Delta t_i$$

$$\boxed{a_H = \ln(\hat{\mu}_H) - \ln(C_{time})}$$

$$\boxed{a_A = \ln(\hat{\mu}_A) - \ln(C_{time})}$$

### 예상 경기 시간 $T_{exp}$

$$T_{exp} = 90 + \mathbb{E}[\alpha_1] + \mathbb{E}[\alpha_2]$$

$\mathbb{E}[\alpha_1], \mathbb{E}[\alpha_2]$는 Phase 1 Step 1.1에서 Goalserve `addedTime_period1/2`의
리그별 평균으로 산출한 값이다.

| 구간 i | 커버 범위 | $\Delta t_i$ |
|--------|----------|-------------|
| 1 | 전반 0~15분 | 15 |
| 2 | 전반 15~30분 | 15 |
| 3 | 전반 30~45분 + 추가시간 | $15 + \mathbb{E}[\alpha_1]$ |
| 4 | 후반 0~15분 | 15 |
| 5 | 후반 15~30분 | 15 |
| 6 | 후반 30~45분 + 추가시간 | $15 + \mathbb{E}[\alpha_2]$ |

### δ와의 관계

킥오프 시점에 ΔS = 0이므로 δ(0) = 0.
**역산 공식에 δ가 영향을 주지 않는다.**
δ는 Phase 3에서 골이 발생한 후부터 활성화된다.

### 결과물

$a_H$, $a_A$, $C_{time}$

---

## Step 2.4: Pre-Match Sanity Check

### 목표

모델의 프리매치 확률이 시장 합의와 과도하게 괴리되지 않는지 검증한다.
Goalserve Pregame Odds의 20+ 북메이커, 50+ 마켓을 활용하여
원래 설계보다 정밀한 다중 차원 검증을 수행한다.

### 1차 검증: Match Winner (Pinnacle 기준)

```python
def primary_sanity_check(mu_H: float, mu_A: float,
                          pinnacle_prob: Tuple[float, float, float],
                          market_avg: Tuple[float, float, float]) -> str:
    """
    모델 확률 vs Pinnacle + 시장 평균 비교.
    Pinnacle이 가장 효율적인 시장이므로 1차 기준으로 사용.
    """
    # 모델 확률 (독립 Poisson)
    P_model = compute_match_odds_poisson(mu_H, mu_A)  # {H, D, A}

    # Pinnacle 괴리도
    delta_pin = max(
        abs(P_model[o] - pinnacle_prob[i])
        for i, o in enumerate(["H", "D", "A"])
    )

    # 시장 평균 괴리도
    delta_mkt = max(
        abs(P_model[o] - market_avg[i])
        for i, o in enumerate(["H", "D", "A"])
    )

    if delta_pin < 0.15:
        return "GO"
    elif delta_pin < 0.25:
        # Pinnacle과 괴리가 있지만 시장 평균과는 가까운 경우
        # → Pinnacle이 일시적 outlier일 수 있음
        if delta_mkt < 0.10:
            return "GO_WITH_CAUTION"
        return "HOLD"
    else:
        return "SKIP"
```

### 2차 검증: Over/Under 교차 확인 (Goalserve 고유)

Goalserve Pregame Odds는 50+ 마켓을 제공하므로,
모델의 μ_H + μ_A가 Over/Under 시장과도 정합하는지 교차 검증한다.

```python
def secondary_sanity_check(mu_H: float, mu_A: float,
                            ou_odds: dict) -> dict:
    """
    모델의 총 기대 득점이 O/U 시장과 정합하는지 확인.
    Match Winner만으로는 못 잡는 '합은 맞지만 비율이 잘못된' 경우를 탐지.
    """
    mu_total = mu_H + mu_A

    # 모델의 Over 2.5 확률
    from scipy.stats import poisson
    P_model_over25 = 1 - poisson.cdf(2, mu_total)

    # 시장의 Over 2.5 내재 확률
    over_odds = float(ou_odds["Over"]["value"])
    under_odds = float(ou_odds["Under"]["value"])
    ou_sum = 1/over_odds + 1/under_odds
    P_market_over25 = (1/over_odds) / ou_sum

    delta_ou = abs(P_model_over25 - P_market_over25)

    return {
        "P_model_over25": P_model_over25,
        "P_market_over25": P_market_over25,
        "delta_ou": delta_ou,
        "ou_consistent": delta_ou < 0.15,
    }
```

**교차 검증의 의미:**

| 1차 (Match Winner) | 2차 (Over/Under) | 진단 |
|-------------------|-------------------|------|
| GO | 일치 | ✅ μ_H, μ_A 모두 정확 |
| GO | 불일치 | ⚠️ μ의 합은 맞지만 비율이 잘못됐을 가능성 |
| HOLD | 일치 | ⚠️ Match Winner 마켓 특이점 (컵전 등) |
| SKIP | — | ❌ 해당 경기 스킵 |

### 최종 판정 결합

```python
def combined_sanity_check(mu_H, mu_A, odds_data) -> SanityResult:
    primary = primary_sanity_check(mu_H, mu_A, ...)
    secondary = secondary_sanity_check(mu_H, mu_A, ...)

    if primary == "SKIP":
        return SanityResult(verdict="SKIP")

    if primary == "GO" and secondary["ou_consistent"]:
        return SanityResult(verdict="GO")

    if primary == "GO" and not secondary["ou_consistent"]:
        return SanityResult(
            verdict="GO_WITH_CAUTION",
            warning="O/U mismatch — μ ratio may be off"
        )

    if primary == "HOLD":
        return SanityResult(verdict="HOLD")

    return SanityResult(verdict="GO_WITH_CAUTION")
```

### 결과물

```python
@dataclass
class SanityResult:
    verdict: str            # GO | GO_WITH_CAUTION | HOLD | SKIP
    delta_match_winner: float   # Pinnacle 괴리도
    delta_over_under: float     # O/U 괴리도
    warning: Optional[str]      # 경고 메시지
```

---

## Step 2.5: 라이브 엔진 초기화 및 연결 확립 (System Initialization)

### 파라미터 로드 및 인스턴스화

```
LiveFootballQuantModel 초기 상태:
│
├── 시간 상태
│   ├── current_time        = 0
│   ├── engine_phase        = WAITING_FOR_KICKOFF
│   └── T_exp               ← Step 2.3
│
├── 경기 상태
│   ├── current_state (X)   = 0  (11v11)
│   ├── current_score (S)   = (0, 0)
│   └── delta_S             = 0
│
├── 강도 함수 파라미터
│   ├── a_H, a_A            ← Step 2.3
│   ├── b[1..6]             ← Phase 1
│   ├── γ^H[0..3]           ← Phase 1 (γ^H_0=0, γ^H_1, γ^H_2, γ^H_1+γ^H_2)
│   ├── γ^A[0..3]           ← Phase 1 (γ^A_0=0, γ^A_1, γ^A_2, γ^A_1+γ^A_2)
│   ├── δ_H[5], δ_A[5]     ← Phase 1
│   └── C_time              ← Step 2.3
│
├── 마르코프 모델
│   ├── Q (4×4)             ← Phase 1
│   ├── Q_off_normalized    ← 아래에서 계산 (하나만, 팀 독립)
│   └── P_grid[0..100]     ← 행렬 지수함수 사전 계산
│
├── Phase 3 모드 제어
│   ├── DELTA_SIGNIFICANT   ← Phase 1 Step 1.5 LRT 결과
│   └── preliminary_cache   = {}  (Phase 3 사전 계산 캐시)
│
├── 이벤트 상태 머신 (Phase 3용)
│   ├── event_state         = IDLE
│   ├── cooldown            = False
│   └── ob_freeze           = False
│
├── 호가 이상 감지 (3-Layer)
│   ├── P_kalshi_prev       = None  (Kalshi 호가 센서)
│   ├── bet365_odds_prev    = None  (Live Odds 센서)
│   └── bet365_score_prev   = None  (Live Odds 스코어 센서)
│
├── Goalserve 연결
│   ├── match_id            ← Step 2.1 (전 Phase 통일 ID)
│   ├── live_score_ready    = False (REST 폴링 검증 후 True)
│   ├── live_odds_ws        = None  (WebSocket 연결 후 할당)
│   └── live_odds_healthy   = False (연결 검증 후 True)
│
├── Kalshi 연결
│   ├── kalshi_ws           = None  (WebSocket 연결 후 할당)
│   └── kalshi_healthy      = False
│
├── Sanity Check 결과
│   ├── verdict             ← Step 2.4
│   ├── delta_match_winner  ← Step 2.4
│   └── delta_over_under    ← Step 2.4
│
└── 리스크 파라미터
    ├── bankroll            ← 현재 Kalshi 계좌 잔고
    ├── f_order_cap  = 0.03
    ├── f_match_cap  = 0.05
    └── f_total_cap  = 0.20
```

### 행렬 지수함수 사전 계산

Phase 3 Step 3.2의 해석적 μ 계산에서 P_grid를 조회하여 O(1) 연산을 달성한다:

```python
import scipy.linalg

P_grid = {}
for dt_min in range(0, 101):
    P_grid[dt_min] = scipy.linalg.expm(Q * dt_min)
```

> **경기 종료 직전 해상도 한계:** P_grid가 정수 분 단위이므로,
> 잔여 시간이 1분 미만일 때 상대 오차가 커질 수 있다.
> Phase 3의 MC 모드에서는 내부에서 직접 퇴장을 시뮬레이션하므로 이 문제가 없다.
> 해석적 모드에서 경기 종료 5분 전부터는 10초 단위 fine grid를 추가 사전 계산하는 것을 권장:

```python
# Fine grid: 경기 종료 직전 5분 (0.0~5.0분, 0.167분 간격)
P_fine_grid = {}
for dt_10sec in range(0, 31):  # 0~30 (= 0~5분, 10초 단위)
    dt_min = dt_10sec / 6.0
    P_fine_grid[dt_10sec] = scipy.linalg.expm(Q * dt_min)
```

### Q_off 전이 확률 정규화

Phase 1에서 도출한 Q 행렬의 비대각 성분을 MC 시뮬레이션용 전이 확률로 정규화:

```python
Q_off_normalized = np.zeros((4, 4))
for i in range(4):
    total_off_diag = -Q[i, i]
    if total_off_diag > 0:
        for j in range(4):
            if i != j:
                Q_off_normalized[i, j] = Q[i, j] / total_off_diag
```

> **Q_off는 팀 독립:** Q 행렬은 마르코프 상태 X(t)의 전이율이며, 이는 팀과 무관하다.
> γ^H, γ^A처럼 팀별로 분리할 필요가 없다. Q_off_normalized **하나만** 생성한다.

### 연결 확립 — 3-소스 아키텍처

| 연결 대상 | 프로토콜 | 지연 | Phase 3 역할 |
|----------|---------|------|-------------|
| **Goalserve Live Odds** | **WebSocket PUSH** | **<1초** | **1차 이벤트 감지 + ob_freeze** |
| Kalshi API | WebSocket | 1~2초 | 호가 수신 + 거래 |
| **Goalserve Live Score** | **REST 폴링 3초** | 3~8초 | **권위적 확인 (VAR, 득점자)** |

**Goalserve Live Odds WebSocket 연결:**

```python
async def connect_live_odds(api_key: str, match_id: str):
    """
    Goalserve Live Odds WebSocket 연결.
    bet365 인플레이 배당 + 경기 info(스코어, 분, 상태) 수신.
    
    Phase 3에서의 역할:
    1. 스코어 변동 감지 (<1초) → preliminary 이벤트
    2. 배당 급변 감지 → ob_freeze
    3. 피리어드 변경 감지 → 하프타임 처리
    """
    ws = await websockets.connect(
        f"wss://goalserve.com/liveodds/{api_key}/{match_id}"
    )
    
    # 연결 검증: 첫 메시지 수신 확인
    first_msg = await asyncio.wait_for(ws.recv(), timeout=10)
    parsed = json.loads(first_msg)
    
    if "info" not in parsed:
        raise ConnectionError("Live Odds: unexpected message format")
    
    log.info(f"Live Odds WS connected: match={match_id}, "
             f"score={parsed['info']['score']}")
    return ws
```

**Goalserve Live Score REST 검증:**

```python
async def verify_live_score(api_key: str, match_id: str):
    """
    Goalserve Live Score REST 엔드포인트 접근 가능 여부 확인.
    IP 화이트리스트 인증.
    """
    url = f"http://www.goalserve.com/getfeed/{api_key}/soccerlive/home"
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url, params={"json": 1})
        response.raise_for_status()
        data = response.json()
        match_found = find_match_in_feed(data, match_id)
        return match_found
```

> **IP 화이트리스트 주의:** Goalserve는 IP 기반 인증을 사용한다.
> 클라우드 배포 시 서버 IP를 Goalserve에 사전 등록해야 한다.

**Kalshi WebSocket 연결:**

```python
async def connect_kalshi(api_key: str, market_tickers: List[str]):
    """Kalshi WebSocket 연결 — 호가 수신 + 거래"""
    ws = await kalshi_api.connect_ws(api_key)
    for ticker in market_tickers:
        await ws.subscribe_orderbook(ticker)
    return ws
```

### Numba JIT 워밍업

Phase 3 Step 3.4의 MC 시뮬레이션은 Numba JIT으로 컴파일된다.
첫 호출 시 ~2초의 컴파일 시간이 발생하므로, 더미 호출로 워밍업한다:

```python
from phase3.mc_core import mc_simulate_remaining

_ = mc_simulate_remaining(
    t_now=0, T_end=1,
    S_H=0, S_A=0,
    state=0, score_diff=0,
    a_H=0.0, a_A=0.0,
    b=np.zeros(6),
    gamma_H=np.zeros(4), gamma_A=np.zeros(4),
    delta_H=np.zeros(5), delta_A=np.zeros(5),
    Q_diag=np.zeros(4), Q_off=Q_off_normalized,
    basis_bounds=np.zeros(7),
    N=10, seed=42
)
log.info("Numba JIT warmup complete")
```

### Goalserve 이벤트 → Phase 3 엔진 매핑

**Live Odds WebSocket (1차 감지, <1초):**

| Live Odds 데이터 변화 | 감지 방식 | Phase 3 처리 |
|----------------------|----------|-------------|
| `info.score` 변동 | 이전 Push와 비교 | **PRELIMINARY goal 감지 + ob_freeze** |
| 배당 급변 (>10%) | 이전 Push와 비교 | **ob_freeze (골 or 레드카드 불명)** |
| `info.period` 변경 | 이전 Push와 비교 | 하프타임 진입/종료 |
| `info.minute` > 45 or > 90 | 필드 값 모니터링 | 추가시간 T 롤링 |

**Live Score REST (권위적 확인, 3~8초):**

| Live Score 데이터 변화 | 감지 방식 | Phase 3 처리 |
|----------------------|----------|-------------|
| score 증가 | 이전 폴링과 diff | **CONFIRMED 골 + 득점자 + VAR 상태** |
| redcards 목록 추가 | 이전 폴링과 diff | **CONFIRMED 레드카드** |
| period 변경 | "1st" → "Half" → "2nd" | 하프타임 교차 확인 |
| status "Finished" | 필드 변경 | 최종 정산 |

### Circuit Breaker

| 장애 유형 | 감지 방법 | 대응 |
|----------|----------|------|
| **Goalserve Live Odds WS 끊김** | **heartbeat 미수신 5초** | **1차 감지 비활성화, Live Score + Kalshi로 fallback** |
| Goalserve Live Score 폴링 실패 | HTTP 3회 연속 실패 | 신규 주문 중단, 5회 시 경기 스킵 |
| Kalshi WS 끊김 | heartbeat 미수신 10초 | 미체결 주문 취소, 재연결 시도 |
| 라인업 변경 | 킥오프 전 재확인 | Step 2.1~2.3 재실행 |
| 경기 연기/취소 | Goalserve status 변경 | 전체 셧다운, 포지션 청산 |

> **Graceful Degradation:** Live Odds WebSocket은 **보조 → 핵심**으로 승격되었지만,
> 이것이 죽어도 시스템은 Live Score REST + Kalshi ob_freeze로 운용 가능하다.
> 성능은 원래 설계 수준으로 떨어지지만, 안전성은 유지된다.

### Pre-Kickoff 최종 확인 (킥오프 5분 전)

```python
async def pre_kickoff_final_check(model: LiveFootballQuantModel):
    """킥오프 5분 전 — 모든 조건 최종 확인"""

    # 1. 라인업 재확인
    current_lineup = await fetch_lineup(model.match_id)
    if current_lineup != model.home_starting_11 + model.away_starting_11:
        log.warning("Lineup changed — re-running Steps 2.1~2.3")
        await re_initialize(model, current_lineup)

    # 2. 연결 상태 확인
    assert model.live_odds_healthy or model.live_score_ready, \
        "At least one Goalserve source must be healthy"
    assert model.kalshi_healthy, "Kalshi WS must be connected"

    # 3. Sanity Check 결과 확인
    if model.sanity_verdict == "SKIP":
        log.info(f"Match {model.match_id} SKIPPED by sanity check")
        return False

    # 4. Numba 워밍업 확인 (이미 완료)
    log.info(f"Match {model.match_id} ready for kickoff")
    return True
```

### 결과물

가동 준비 완료된 `LiveFootballQuantModel` 인스턴스.

---

## Phase 2 → Phase 3 핸드오프

| 항목 | 값 | 출처 |
|------|---|------|
| $a_H, a_A$ | 초기 득점 강도 | Step 2.3 |
| $\mathbf{b}[1..6]$ | 시간 구간별 프로파일 | Phase 1 |
| $\gamma^H[0..3], \gamma^A[0..3]$ | 팀별 퇴장 패널티 | Phase 1 |
| $\boldsymbol{\delta}_H[5], \boldsymbol{\delta}_A[5]$ | 스코어차 효과 | Phase 1 |
| Q (4×4) | 마르코프 전이 행렬 | Phase 1 |
| $C_{time}$, $T_{exp}$ | 시간 상수 | Step 2.3 |
| $P_{grid}[0..100]$ + $P_{fine\_grid}$ | 행렬 지수함수 그리드 | Step 2.5 |
| $Q_{off\_normalized}$ (4×4) | MC용 정규화 전이 확률 (하나, 팀 독립) | Step 2.5 |
| `DELTA_SIGNIFICANT` | δ 유의성 → 해석적/MC 모드 결정 | Phase 1 Step 1.5 |
| 시스템 상태 | t=0, X=0, S=(0,0), ΔS=0 | Step 2.5 |
| event_state | IDLE (초기) | Step 2.5 |
| **Goalserve match_id** | **전 Phase 통일 경기 ID** | Step 2.1 |
| **Goalserve Live Odds WS** | **1차 이벤트 감지 + ob_freeze** | Step 2.5 |
| Goalserve Live Score | REST 폴링 준비 | Step 2.5 |
| Kalshi WS | 호가 수신 + 거래 준비 | Step 2.5 |
| ob_freeze | False (3-Layer 센서 초기화) | Step 2.5 |
| cooldown | False | Step 2.5 |
| Sanity 결과 | GO / GO_WITH_CAUTION / HOLD / SKIP | Step 2.4 |
| 리스크 한도 | f_order=0.03, f_match=0.05, f_total=0.20 | Step 2.5 |

---

## Phase 2 파이프라인 요약

```
[킥오프 60분 전: 라인업 발표]
              │
              ▼
┌──────────────────────────────────────────────────────────────┐
│  Step 2.1: 데이터 수집 (Goalserve 일원화)                      │
│                                                              │
│  2.1.1: Live Game Stats → 선발 11명 + 포메이션               │
│  2.1.2: 과거 player_stats → 선수별 롤링 per-90              │
│  2.1.3: 포지션별 팀 레벨 집계 (G/D/M/F)                      │
│  2.1.4: 과거 stats.{team} → 팀 롤링 (xG 포함)               │
│  2.1.5: Pregame Odds → 20+ 북메이커 배당률 피처              │
│  2.1.6: Fixtures → 컨텍스트 (휴식일, H2H)                    │
│                                                              │
│  Output: PreMatchData (Tier 1~4 피처 + 라인업 + 배당)         │
└──────────────────┬───────────────────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────────────────┐
│  Step 2.2: 피처 선택                                          │
│  • feature_mask.json 적용 (Phase 1과 동일 Goalserve 스키마)   │
│  • 결측값 → Phase 1 학습 median 대체                         │
│  • 수동 매핑 레이어 불필요 (일원화 효과)                       │
│  Output: X_match ∈ ℝ^{d'}                                   │
└──────────────────┬───────────────────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────────────────┐
│  Step 2.3: a 파라미터 역산                                    │
│  • XGBoost Poisson → μ̂_H, μ̂_A                             │
│  • a = ln(μ̂) − ln(C_time)                                  │
│  Output: a_H, a_A, C_time                                    │
└──────────────────┬───────────────────────────────────────────┘
                   ▼
┌──────────────────────────────────────────────────────────────┐
│  Step 2.4: Sanity Check (다중 차원)                           │
│  • 1차: Match Winner vs Pinnacle + 시장 평균                  │
│  • 2차: Over/Under 교차 검증 (50+ 마켓 활용)                  │
│  Output: GO / GO_WITH_CAUTION / HOLD / SKIP                  │
└──────────────────┬───────────────────────────────────────────┘
                   │ [GO or GO_WITH_CAUTION]
                   ▼
┌──────────────────────────────────────────────────────────────┐
│  Step 2.5: 시스템 초기화                                      │
│                                                              │
│  파라미터 로드:                                               │
│  • Phase 1 파라미터 (b, γ^H, γ^A, δ_H, δ_A, Q)             │
│  • P_grid[0..100] + P_fine_grid 사전 계산                    │
│  • Q_off_normalized 정규화 (하나, 팀 독립)                    │
│                                                              │
│  연결 확립 (3-소스):                                          │
│  • Goalserve Live Odds WebSocket (<1초) ← 1차 감지          │
│  • Kalshi WebSocket (1~2초) ← 호가 + 거래                   │
│  • Goalserve Live Score REST (3~8초) ← 권위적 확인           │
│                                                              │
│  안전장치:                                                    │
│  • Numba JIT 워밍업                                          │
│  • Circuit Breaker 활성화                                    │
│  • 3-Layer ob_freeze 센서 초기화                              │
│  • 킥오프 5분 전 최종 확인                                    │
│                                                              │
│  Output: LiveFootballQuantModel 인스턴스                      │
└──────────────────────────────────────────────────────────────┘
              │
              ▼
        [Phase 3: Live Trading Engine 시작]
        (3-Layer 감지: Live Odds WS + Kalshi WS + Live Score REST)
```
