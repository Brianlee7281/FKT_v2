# Phase 4: Arbitrage & Execution — Goalserve Full Package

## 개요

앞선 3개의 Phase를 거쳐 정교하게 깎인 수학적 확률(P_true)을
실제 시장의 돈과 맞바꾸는 트레이딩(Execution)의 최전선.

아무리 모델이 완벽해도 이 단계에서 호가창의 마찰 비용이나
자금 관리(Money Management)를 잘못 설계하면 시스템은 파산(Ruin)을 맞이한다.

Phase 3 엔진이 산출한 확률을 바탕으로 Kalshi 시장에서 엣지(Edge)를 찾아내고,
켈리 공식에 따라 주문을 전송하며,
경기 종료 후 모델 성능을 피드백하는 전 과정을
6개의 Step으로 분해한다.

### 패러다임 전환: 방어적 → 적응적

Goalserve 풀 패키지의 3-Layer 감지 체계가 Phase 4의 전략을 근본적으로 바꾼다:

```
원래: 이벤트 감지(3~8초) > Kalshi MM(1~2초) → 항상 방어적
풀패키지: 이벤트 감지(<1초) ≤ Kalshi MM(1~2초) → 상황에 따라 공격적 가능
```

이건 진입 타이밍, 사이징, 청산 전부에 영향을 준다.
다만 공격성은 Phase 0 → A → B → C 로드맵에 따라 **점진적으로** 높인다.

---

## Input Data

**Phase 3 산출물 (매 1초):**

| 항목 | 설명 | 업데이트 빈도 |
|------|------|-------------|
| P_true(t) | 각 시장별 진짜 확률 | 매 1초 |
| σ_MC(t) | Monte Carlo 표준오차 (해석적: 0) | 매 1초 |
| order_allowed | NOT cooldown AND NOT ob_freeze AND event_state == IDLE | 매 1초 + 이벤트 시 |
| event_state | IDLE / PRELIMINARY / CONFIRMED | 이벤트 시 |
| pricing_mode | Analytical / Monte Carlo | 이벤트 시 전환 |
| engine_phase | FIRST_HALF / HALFTIME / SECOND_HALF / FINISHED | 피리어드 변경 시 |
| μ_H, μ_A | 잔여 기대 득점 | 매 1초 (로깅용) |
| **P_bet365(t)** | **bet365 인플레이 내재 확률 (시장별)** | **매 Push (<1초)** |

**Kalshi API:**

| 엔드포인트 | 용도 |
|-----------|------|
| WebSocket | 실시간 호가창 (Bid/Ask + Depth) |
| REST `/portfolio/orders` | 주문 제출/취소 |
| REST `/portfolio/positions` | 기존 포지션 조회 |
| REST `/portfolio/balance` | 계좌 잔고 |

---

## Step 4.1: 실시간 호가창 동기화 (Live Order Book Synchronization)

### 목표

Kalshi 거래소의 호가창 데이터를 실시간으로 수신하고,
Phase 3의 P_true와 동일한 타임스탬프 상에 정렬한다.
Goalserve Live Odds의 bet365 배당을 **독립적인 시장 참조 가격**으로 추가 활용한다.

### Kalshi 호가 수신

**Bid/Ask 분리:**

매수 시에는 Ask, 매도 시에는 Bid를 사용한다.
Bid-Ask 스프레드 자체가 숨겨진 거래 비용이다.

$$P_{kalshi}^{buy} = \frac{\text{Best Ask (¢)}}{100}, \quad P_{kalshi}^{sell} = \frac{\text{Best Bid (¢)}}{100}$$

**호가창 깊이(Depth) — VWAP 실효 가격:**

Best Bid/Ask만 보면 1호가 물량을 초과하는 주문에서 슬리피지가 발생한다.
주문 수량 Q에 대해 가중 평균 실효 가격을 계산한다:

$$P_{effective}(Q) = \frac{\sum_{level} p_{level} \times q_{level}}{\sum_{level} q_{level}} \quad \text{(Q 계약까지 누적)}$$

**유동성 필터:**

호가창 총 물량이 최소 기준 미달이면 진입하지 않는다:

$$\text{Total Ask Depth} \geq Q_{min} \quad (\text{예: } Q_{min} = 20\text{ 계약})$$

### bet365 참조 가격 (Goalserve 고유)

Goalserve Live Odds WebSocket이 bet365 인플레이 배당을 <1초로 제공한다.
이걸 **독립적인 시장 참조 가격**으로 활용한다:

```python
class OrderBookSync:
    def __init__(self):
        # Kalshi 호가
        self.kalshi_best_bid = None
        self.kalshi_best_ask = None
        self.kalshi_depth = []

        # bet365 참조 가격
        self.bet365_implied = {}

    def update_kalshi(self, orderbook: dict):
        """Kalshi WebSocket에서 수신"""
        self.kalshi_best_bid = orderbook["best_bid"] / 100
        self.kalshi_best_ask = orderbook["best_ask"] / 100
        self.kalshi_depth = orderbook["levels"]

    def update_bet365(self, live_odds_markets: dict):
        """
        Goalserve Live Odds WebSocket에서 수신.
        bet365 Fulltime Result + Over/Under 배당 → 내재 확률.
        """
        # Fulltime Result
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

        # Over/Under 마켓 (있으면)
        ou = live_odds_markets.get("over_under_25", {})
        if ou:
            over_odds = float(ou.get("Over", {}).get("value_eu", 0))
            under_odds = float(ou.get("Under", {}).get("value_eu", 0))
            if over_odds > 0 and under_odds > 0:
                ou_sum = 1/over_odds + 1/under_odds
                self.bet365_implied["over_25"] = (1/over_odds) / ou_sum
                self.bet365_implied["under_25"] = (1/under_odds) / ou_sum
```

**bet365 참조 가격의 3가지 용도:**

| 용도 | 방법 | Step |
|------|------|------|
| **Edge 교차 검증** | 모델 vs Kalshi vs bet365 3자 비교 | Step 4.2 |
| **사이징 조정** | bet365 동의 시 풀 사이징, 불동의 시 절반 | Step 4.3 |
| **청산 판단 보조** | bet365가 포지션과 반대 방향이면 경고 | Step 4.4 |

### 결과물

| 항목 | 설명 |
|------|------|
| $P_{kalshi}^{buy}(t)$ | Yes 매수 시 실효 내재 확률 |
| $P_{kalshi}^{sell}(t)$ | Yes 매도 시 실효 내재 확률 |
| **$P_{bet365}(t)$** | **bet365 인플레이 내재 확률 (시장별)** |
| liquidity_ok | 유동성 필터 통과 여부 |
| depth_profile | 호가 레벨별 수량 (VWAP 계산용) |

---

## Step 4.2: Fee-Adjusted Edge 판별 (EV 산출)

### 목표

모델의 P_true와 시장의 P_kalshi를 비교하여,
수수료와 슬리피지를 감안하고도 **양의 기댓값(Positive EV)**이 있는지 검증한다.
bet365를 독립 검증자로 추가하여 Edge의 신뢰도를 분류한다.

### Kalshi 수수료 구조

수수료율을 c라 하자.
수수료는 **수익(Profit)에 대해서만** 부과되며, 손실 시에는 부과되지 않는다.

> **주의:** c의 정확한 값은 Kalshi의 현행 수수료 체계를 확인하여 적용한다.
> 수수료 구조가 변경되면 이 공식을 재조정해야 한다.

### P_true^cons — 방향별 보수적 보정

Phase 3의 Monte Carlo 프라이싱은 유한한 시뮬레이션 횟수 N에서 오는
표준오차 σ_MC를 함께 출력한다. 모델 과신을 방지하기 위해
**방향별 보수적 하한/상한**을 사용한다:

```python
def compute_conservative_P(P_true: float, sigma_MC: float,
                            direction: str, z: float = 1.645) -> float:
    """
    Buy Yes: P가 높을수록 유리 → 하한 사용 (보수적으로 낮춤)
    Buy No:  P가 낮을수록 유리 → 상한 사용 (보수적으로 높임)
    
    z=1.645 → 90% 보수적 (초기 라이브 권장)
    z=1.0   → 68% 보수적 (성숙기)
    """
    if direction == "BUY_YES":
        return P_true - z * sigma_MC
    elif direction == "BUY_NO":
        return P_true + z * sigma_MC
    else:
        return P_true
```

> **왜 방향별로 달라야 하는가:**
> 단일 하한(P_true - z·σ)을 양방향에 쓰면 Buy No에서 (1 - P_cons)가 커져
> No 방향 EV를 **과대평가**한다. MC 불확실성이 클수록 No 방향에 더 공격적으로
> 베팅하는 체계적 오류가 발생한다.
>
> 해석적 모드(σ_MC = 0)에서는 P_cons = P_true이므로 방향 무관.

### Fee-Adjusted EV 공식 — 방향별 분기

```python
def compute_EV(P_true: float, sigma_MC: float,
               P_kalshi_buy: float, P_kalshi_sell: float,
               c: float, z: float) -> Tuple[float, float]:
    """양방향 EV를 동시에 계산"""

    # Buy Yes 방향
    P_cons_yes = P_true - z * sigma_MC
    EV_buy_yes = (
        P_cons_yes * (1 - c) * (1 - P_kalshi_buy)
        - (1 - P_cons_yes) * P_kalshi_buy
    )

    # Buy No 방향 (= Sell Yes)
    P_cons_no = P_true + z * sigma_MC  # ← 상한 사용
    EV_buy_no = (
        (1 - P_cons_no) * (1 - c) * P_kalshi_sell
        - P_cons_no * (1 - P_kalshi_sell)
    )

    return EV_buy_yes, EV_buy_no
```

### 3자 교차 검증 — bet365를 독립 검증자로 (Goalserve 고유)

원래 설계에서는 "모델 vs Kalshi" 2자 비교였다.
풀 패키지에서는 bet365가 제3의 독립적 가격 발견자로 추가된다:

```python
@dataclass
class EdgeValidation:
    confidence: str          # "HIGH", "LOW", "NONE"
    kelly_multiplier: float  # HIGH→1.0, LOW→0.5, NONE→0.0

def validate_edge_with_bet365(
    P_true_cons: float,
    P_kalshi: float,
    P_bet365: float,
    direction: str
) -> EdgeValidation:
    """
    3자 비교로 Edge의 신뢰도를 분류.

    핵심 논리:
    - bet365는 세계 최대 인플레이 배당 시장 → 가장 효율적인 가격
    - 모델과 bet365가 같은 방향이면 Edge 신뢰도 높음
    - 모델만 Edge를 주장하고 bet365가 불동의면 우리가 틀릴 확률 높음
    """
    if P_bet365 is None:
        # bet365 데이터 없음 (Live Odds WS 장애 등) → LOW로 fallback
        return EdgeValidation(confidence="LOW", kelly_multiplier=0.5)

    if direction == "BUY_YES":
        model_says_high = P_true_cons > P_kalshi
        bet365_says_high = P_bet365 > P_kalshi

        if model_says_high and bet365_says_high:
            return EdgeValidation(confidence="HIGH", kelly_multiplier=1.0)
        elif model_says_high and not bet365_says_high:
            return EdgeValidation(confidence="LOW", kelly_multiplier=0.5)
        else:
            return EdgeValidation(confidence="NONE", kelly_multiplier=0.0)

    elif direction == "BUY_NO":
        model_says_low = P_true_cons < P_kalshi
        bet365_says_low = P_bet365 < P_kalshi

        if model_says_low and bet365_says_low:
            return EdgeValidation(confidence="HIGH", kelly_multiplier=1.0)
        elif model_says_low and not bet365_says_low:
            return EdgeValidation(confidence="LOW", kelly_multiplier=0.5)
        else:
            return EdgeValidation(confidence="NONE", kelly_multiplier=0.0)

    return EdgeValidation(confidence="NONE", kelly_multiplier=0.0)
```

### 필터링 조건

시그널 발생 조건 (모든 조건을 동시에 만족해야 함):

| 조건 | 설명 |
|------|------|
| EV_adj > θ_entry | 최소 엣지 임계값 (예: θ_entry = 0.02 = 2¢) |
| order_allowed = True | NOT cooldown AND NOT ob_freeze |
| event_state == IDLE | preliminary 중 아님 |
| liquidity_ok = True | 호가창 최소 물량 충족 |
| engine_phase ∈ {FIRST_HALF, SECOND_HALF} | 하프타임/종료 시 진입 불가 |
| edge_validation.confidence ≠ NONE | bet365 교차 검증 통과 |

### 시그널 생성

```python
@dataclass
class Signal:
    direction: str          # BUY_YES, BUY_NO, HOLD
    EV: float
    P_cons: float
    P_kalshi: float
    bet365_confidence: str  # HIGH, LOW
    kelly_multiplier: float
    market_ticker: str

def generate_signal(P_true: float, sigma_MC: float,
                    P_kalshi_buy: float, P_kalshi_sell: float,
                    P_bet365: float, c: float, z: float,
                    market_ticker: str) -> Signal:
    """양방향 EV 평가 + bet365 교차 검증"""

    EV_buy_yes, EV_buy_no = compute_EV(
        P_true, sigma_MC, P_kalshi_buy, P_kalshi_sell, c, z
    )

    # Buy Yes 방향 평가
    if EV_buy_yes > THETA_ENTRY:
        P_cons_yes = P_true - z * sigma_MC
        edge_val = validate_edge_with_bet365(
            P_cons_yes, P_kalshi_buy, P_bet365, "BUY_YES"
        )
        if edge_val.confidence != "NONE":
            return Signal(
                direction="BUY_YES",
                EV=EV_buy_yes,
                P_cons=P_cons_yes,
                P_kalshi=P_kalshi_buy,
                bet365_confidence=edge_val.confidence,
                kelly_multiplier=edge_val.kelly_multiplier,
                market_ticker=market_ticker
            )

    # Buy No 방향 평가
    if EV_buy_no > THETA_ENTRY:
        P_cons_no = P_true + z * sigma_MC
        edge_val = validate_edge_with_bet365(
            P_cons_no, P_kalshi_sell, P_bet365, "BUY_NO"
        )
        if edge_val.confidence != "NONE":
            return Signal(
                direction="BUY_NO",
                EV=EV_buy_no,
                P_cons=P_cons_no,
                P_kalshi=P_kalshi_sell,
                bet365_confidence=edge_val.confidence,
                kelly_multiplier=edge_val.kelly_multiplier,
                market_ticker=market_ticker
            )

    return Signal(direction="HOLD", EV=0, P_cons=P_true,
                  P_kalshi=0, bet365_confidence="NONE",
                  kelly_multiplier=0, market_ticker=market_ticker)
```

### 결과물

```python
Signal(direction, EV, P_cons, P_kalshi, bet365_confidence, kelly_multiplier, market_ticker)
```

---

## Step 4.3: 포지션 사이징 — Fee-Adjusted Kelly Criterion

### 목표

엣지가 있다고 전 재산을 베팅할 수는 없으므로,
파산 확률을 0으로 만들면서 장기 복리 수익률을 극대화하는
최적의 투자 비중 f*를 계산한다.

### 방향별 Fee-Adjusted Kelly

P_cons가 이미 방향별로 보정되어 있으므로,
Kelly 공식도 방향에 맞는 W/L을 사용한다:

```python
def compute_kelly(signal: Signal, c: float, K_frac: float) -> float:
    """
    방향별 P_cons + bet365 신뢰도 반영 Kelly.
    """
    P_cons = signal.P_cons
    P_kalshi = signal.P_kalshi

    if signal.direction == "BUY_YES":
        W = (1 - c) * (1 - P_kalshi)   # 승리 시 순수익률
        L = P_kalshi                     # 패배 시 손실률
    elif signal.direction == "BUY_NO":
        W = (1 - c) * P_kalshi           # No 승리 시 순수익률
        L = (1 - P_kalshi)               # No 패배 시 손실률
    else:
        return 0.0

    if W * L <= 0:
        return 0.0

    f_kelly = signal.EV / (W * L)

    # Fractional Kelly
    f_invest = K_frac * f_kelly

    # bet365 교차 검증에 의한 추가 조정
    f_invest *= signal.kelly_multiplier
    # HIGH confidence → 1.0 (그대로)
    # LOW confidence  → 0.5 (절반)

    return max(0.0, f_invest)
```

### Fractional Kelly 정책

| K_frac | 성장률 (Full 대비) | 변동성 (Full 대비) | 권장 상황 |
|--------|-------------------|-------------------|----------|
| 0.50 | 75% | 50% 감소 | Brier Score 우수, 100+ 거래 축적 후 |
| 0.25 | 44% | 75% 감소 | **초기 라이브 테스트 기간 (권장 시작점)** |

Full Kelly(K_frac = 1.0)는 **사용하지 않는다** — 모델 오차에 대한 안전마진 유지.

### 동일 경기 내 상관 포지션 제한

같은 경기에서 "Home Win" YES와 "Over 2.5" YES를 동시에 보유하면,
홈팀이 3-0으로 이기는 시나리오에서 두 포지션 모두 승리한다.
독립 Kelly를 각각 적용하면 이 시나리오에 과도하게 노출된다.

**해법 — 경기별 포지션 한도:**

$$\sum_{\text{markets in match}} |f_{invest,i}| \leq f_{match\_cap}$$

초과 시 비례 축소(pro-rata scaling):

$$f_{invest,i}^{scaled} = f_{invest,i} \times \frac{f_{match\_cap}}{\sum_i |f_{invest,i}|}$$

### 3-Layer 리스크 한도

3단계 리스크 한도를 **동시에** 적용한다:

```python
def apply_risk_limits(f_invest: float, match_id: str,
                      bankroll: float) -> float:
    """3-Layer 리스크 한도 적용"""
    amount = f_invest * bankroll

    # Layer 1: 단일 주문 ≤ 3%
    amount = min(amount, bankroll * F_ORDER_CAP)

    # Layer 2: 경기별 ≤ 5%
    current_match_exposure = get_match_exposure(match_id)
    remaining_match = bankroll * F_MATCH_CAP - current_match_exposure
    amount = min(amount, max(0, remaining_match))

    # Layer 3: 전체 포트폴리오 ≤ 20%
    total_exposure = get_total_exposure()
    remaining_total = bankroll * F_TOTAL_CAP - total_exposure
    amount = min(amount, max(0, remaining_total))

    return amount
```

| Layer | 파라미터 | 기본값 | 의미 |
|-------|---------|--------|------|
| 1 | f_order_cap | 0.03 (3%) | 한 번의 주문으로 자본의 3% 초과 불가 |
| 2 | f_match_cap | 0.05 (5%) | 한 경기에 자본의 5% 초과 불가 |
| 3 | f_total_cap | 0.20 (20%) | 동시 진행 모든 경기 총 노출 20% 초과 불가 |

**한도 초과 시:**

| Layer 초과 | 조치 |
|-----------|------|
| Layer 1 | 주문량을 f_order_cap × Bankroll로 클램핑 |
| Layer 2 | 해당 경기 추가 진입 차단. 기존 포지션 유지. |
| Layer 3 | **모든 경기** 추가 진입 차단. 기존 포지션 유지. |

### 최종 투입 금액

$$\text{Amount}_i = \text{apply\_risk\_limits}(f_{invest,i}^{scaled}, \text{match\_id}, \text{Bankroll})$$

$$\text{Contracts}_i = \left\lfloor \frac{\text{Amount}_i}{P_{kalshi,i}} \right\rfloor$$

### 결과물

각 시장별 최종 투입 금액과 계약 수량.

---

## Step 4.4: 포지션 청산 로직 (Exit Signal)

### 목표

경기 중 상황 변화로 엣지가 사라지거나 역전되면 기존 포지션을 닫아야 한다.
진입만 있고 청산이 없으면 모델이 틀렸을 때 손실이 무제한으로 확대된다.

### 청산 트리거 — 4개 (원래 3개 + bet365 이탈 경고)

**트리거 1 — Edge 소멸:**

```python
def check_edge_decay(position, P_true, sigma_MC, P_kalshi_bid, c, z):
    """EV가 0.5¢ 미만으로 줄어들면 포지션 유지 이유 없음"""
    if position.direction == "BUY_YES":
        P_cons = P_true - z * sigma_MC
    else:
        P_cons = P_true + z * sigma_MC

    current_EV = compute_position_EV(P_cons, P_kalshi_bid, position, c)
    if current_EV < THETA_EXIT:  # 0.005 = 0.5¢
        return ExitSignal(reason="EDGE_DECAY", EV=current_EV)
    return None
```

**트리거 2 — Edge 역전:**

```python
def check_edge_reversal(position, P_true, sigma_MC, P_kalshi_bid, z):
    """모델이 시장보다 반대 방향으로 평가하면 즉시 청산"""
    if position.direction == "BUY_YES":
        P_cons = P_true - z * sigma_MC
        if P_cons < P_kalshi_bid - THETA_ENTRY:
            return ExitSignal(reason="EDGE_REVERSAL")
    elif position.direction == "BUY_NO":
        P_cons = P_true + z * sigma_MC
        if P_cons > (1 - P_kalshi_bid) + THETA_ENTRY:
            return ExitSignal(reason="EDGE_REVERSAL")
    return None
```

**트리거 3 — 시간 기반 만기 평가 (종료 3분 전):**

```python
def check_expiry_eval(position, P_true, sigma_MC, P_kalshi_bid, c, z, t, T):
    """경기 종료 직전: 만기 보유 vs 현재가 청산 비교"""
    if T - t >= 3:
        return None  # 아직 이르다

    if position.direction == "BUY_YES":
        P_cons = P_true - z * sigma_MC
    else:
        P_cons = P_true + z * sigma_MC

    E_hold = P_cons * (1 - c) * (1 - position.entry_price) \
             - (1 - P_cons) * position.entry_price

    profit_if_exit = P_kalshi_bid - position.entry_price
    fee_if_exit = c * max(0, profit_if_exit)
    E_exit = profit_if_exit - fee_if_exit

    if E_exit > E_hold:
        return ExitSignal(reason="EXPIRY_EVAL", E_hold=E_hold, E_exit=E_exit)
    return None
```

**트리거 4 (신규) — bet365 이탈 경고:**

```python
@dataclass
class DivergenceAlert:
    severity: str           # "WARNING"
    P_bet365: float
    P_entry: float
    suggested_action: str   # "REDUCE_OR_EXIT"

def check_bet365_divergence(position, P_bet365: float) -> Optional[DivergenceAlert]:
    """
    보유 포지션 방향과 bet365가 반대로 움직이면 경고.
    
    자동 청산이 아닌 경고로 시작.
    Step 4.6에서 "경고 후 청산했어야 하는가"를 사후 분석하여
    데이터가 축적되면 자동 청산으로 업그레이드 가능.
    """
    if P_bet365 is None:
        return None

    if position.direction == "BUY_YES":
        # Yes 보유 중인데 bet365가 5¢ 이상 하락
        if P_bet365 < position.entry_price - 0.05:
            return DivergenceAlert(
                severity="WARNING",
                P_bet365=P_bet365,
                P_entry=position.entry_price,
                suggested_action="REDUCE_OR_EXIT"
            )

    elif position.direction == "BUY_NO":
        # No 보유 중인데 bet365가 상승 (= No 가치 하락)
        if P_bet365 > (1 - position.entry_price) + 0.05:
            return DivergenceAlert(
                severity="WARNING",
                P_bet365=P_bet365,
                P_entry=position.entry_price,
                suggested_action="REDUCE_OR_EXIT"
            )

    return None
```

### 전체 청산 평가 루프

```python
async def evaluate_exit(position, P_true, sigma_MC, P_kalshi_bid,
                        P_bet365, c, z, t, T) -> Optional[ExitSignal]:
    """매 틱에서 모든 보유 포지션에 대해 호출"""

    # 트리거 1: Edge 소멸
    exit = check_edge_decay(position, P_true, sigma_MC, P_kalshi_bid, c, z)
    if exit:
        return exit

    # 트리거 2: Edge 역전
    exit = check_edge_reversal(position, P_true, sigma_MC, P_kalshi_bid, z)
    if exit:
        return exit

    # 트리거 3: 만기 평가
    exit = check_expiry_eval(position, P_true, sigma_MC, P_kalshi_bid, c, z, t, T)
    if exit:
        return exit

    # 트리거 4: bet365 이탈 경고 (로깅만, 자동 청산 아님)
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
        # 자동 청산 활성화 여부는 Step 4.6 분석 후 결정
        if BET365_DIVERGENCE_AUTO_EXIT:
            return ExitSignal(reason="BET365_DIVERGENCE")

    return None  # 보유 유지
```

### 청산 주문 실행

빠른 체결을 우선하는 공격적 지정가:

| 청산 방향 | 주문 가격 | 미체결 시 |
|----------|----------|----------|
| Yes 매도 | Bid - 1¢ | 2초 후 Bid 가격으로 재제출 |
| No 매도 | Bid - 1¢ | 2초 후 Bid 가격으로 재제출 |

### 결과물

청산 완료 후 포지션 수량 감소 및 실현 손익(Realized P&L) 기록.

---

## Step 4.5: 주문 실행 및 리스크 관리 (Execution & Risk Management)

### 목표

계산된 수량만큼 Kalshi 거래소에 주문을 전송하고,
3단계 리스크 한도를 실시간으로 관리한다.

### 주문 실행

**주문 타입 선택:**

| 상황 | 주문 타입 | 이유 |
|------|----------|------|
| 일반 진입 | Limit Order (Ask + 0~1¢) | 체결 확률과 슬리피지의 균형 |
| 긴급 청산 | Limit Order (Bid - 1¢) | 빠른 체결 우선 |
| **Rapid Entry** | **Limit Order (Ask + 1¢)** | **이벤트 직후 정보 우위 활용 (조건부)** |
| 유동성 부족 | 주문 보류 | 슬리피지 > Edge이면 진입 불가 |

**주문 제출:**

```python
async def execute_order(signal: Signal, amount: float,
                        ob_sync: OrderBookSync,
                        urgent: bool = False) -> Optional[FillResult]:
    """Kalshi REST API로 주문 제출"""

    P_kalshi = signal.P_kalshi
    contracts = int(amount / P_kalshi)

    if contracts < 1:
        return None

    # 주문 가격 결정
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

    # 체결 대기 (5초 타이머)
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
        log.info(f"Order unfilled, cancelled: {order_id}")
        return None
```

### Rapid Entry — 이벤트 직후 빠른 진입 (조건부)

Phase 3의 preliminary 단계에서 이미 μ를 사전 계산하고 있으므로,
confirmed 직후에 쿨다운 전 즉시 진입이 가능하다:

```python
async def post_event_rapid_entry(model, confirmed_event):
    """
    이벤트 확정 직후, 쿨다운 전에 즉시 진입 가능 여부 평가.
    
    핵심: bet365는 이미 반영했지만 Kalshi MM이 아직 반응하지 않은
    0.5~1초 창에서 정보 우위를 활용.
    """
    if not RAPID_ENTRY_ENABLED:
        return  # 초기에는 무조건 비활성화

    # 사전 계산된 P_true 사용
    if not model.preliminary_cache.get("μ_H"):
        return

    P_true = compute_P_from_preliminary(model)
    P_bet365 = model.ob_sync.bet365_implied.get(market_key)
    P_kalshi = model.ob_sync.kalshi_best_ask

    if P_bet365 is None or P_kalshi is None:
        return

    # 3자 비교
    direction = infer_direction(P_true, P_kalshi)
    edge_val = validate_edge_with_bet365(P_true, P_kalshi, P_bet365, direction)

    if edge_val.confidence == "HIGH":
        signal = Signal(
            direction=direction,
            EV=compute_rapid_EV(P_true, P_kalshi, c),
            P_cons=P_true,  # rapid entry에서는 σ_MC ≈ 0 (사전 계산)
            P_kalshi=P_kalshi,
            bet365_confidence="HIGH",
            kelly_multiplier=1.0,
            market_ticker=model.active_market
        )
        amount = compute_kelly(signal, c, K_frac)
        amount = apply_risk_limits(amount, model.match_id, model.bankroll)
        await execute_order(signal, amount, model.ob_sync, urgent=True)
        log.info(f"RAPID ENTRY: {signal.direction}, EV={signal.EV:.4f}")
```

**Rapid Entry 활성화 조건:**

```python
RAPID_ENTRY_ENABLED = (
    cumulative_trades >= 200
    and edge_realization >= 0.8
    and preliminary_accuracy >= 0.95
    and var_cancellation_rate < 0.03
)
```

초기에는 **무조건 비활성화**하고, Step 4.6의 사후 분석에서
"rapid entry를 했다면 수익이었을까"를 시뮬레이션해서
충분한 데이터가 쌓인 후에만 활성화한다.

### 거래 로그

모든 주문(진입 + 청산)에 대해 다음을 기록한다:

```python
@dataclass
class TradeLog:
    # 시간 + 식별
    timestamp: float
    match_id: str                    # Goalserve match_id (전 Phase 통일)
    market_ticker: str

    # 방향 + 타입
    direction: str                   # BUY_YES | BUY_NO | SELL_YES | SELL_NO
    order_type: str                  # ENTRY | EXIT_EDGE_DECAY | EXIT_EDGE_REVERSAL
                                     # | EXIT_EXPIRY_EVAL | EXIT_BET365_DIVERGENCE
                                     # | RAPID_ENTRY

    # 수량 + 가격
    quantity_ordered: int
    quantity_filled: int
    limit_price: float
    fill_price: float

    # 모델 상태
    P_true_at_order: float
    P_true_cons_at_order: float      # 방향별 보수적 P
    P_kalshi_at_order: float
    P_bet365_at_order: float         # bet365 참조 가격
    EV_adj: float
    sigma_MC: float
    pricing_mode: str                # ANALYTICAL | MONTE_CARLO

    # 사이징
    f_kelly: float
    f_invest_scaled: float
    K_frac: float
    bet365_confidence: str           # HIGH | LOW
    kelly_multiplier: float

    # 방어 상태
    cooldown_active: bool
    ob_freeze_active: bool
    event_state: str                 # IDLE | PRELIMINARY | CONFIRMED

    # 계좌
    engine_phase: str
    bankroll_before: float
    bankroll_after: float
```

### 피드백 루프 (실시간)

```
주문 체결
    │
    ├── 포지션 DB 업데이트 (수량, 평균 진입가, 미실현 P&L)
    ├── Bankroll 업데이트 (현금 잔고 변동)
    ├── Layer 1/2/3 한도 재계산
    └── 거래 로그 기록
```

### 결과물

체결 완료된 포지션, 업데이트된 계좌 잔고, 실시간 리스크 대시보드.

---

## Step 4.6: 경기 종료 정산 및 사후 분석 (Post-Match Settlement & Analytics)

### 목표

경기 종료 후 모든 포지션을 정산하고,
모델 성능을 기록하여 Phase 1의 재학습에 피드백한다.
이 단계가 전체 시스템의 **자가 보정(Self-Calibrating) 피드백 루프**를 완성시킨다.

### 자동 정산

Kalshi에서 만기 정산 결과(Yes = 100¢ 또는 No = 0¢)를 수신하면:

$$\text{Realized P\&L}_i = \text{Quantity}_i \times (\text{Settlement}_i - \text{Entry Price}_i) - \text{Fee}_i$$

$$\text{Fee}_i = c \times \max(0,\; \text{Settlement}_i - \text{Entry Price}_i) \times \text{Quantity}_i$$

### 사후 분석 지표 — 11개

#### 원래 지표 (1~6)

**1. 경기별 P&L:**

$$\text{Match P\&L} = \sum_{i \in \text{positions}} \text{Realized P\&L}_i$$

**2. 모델 정확도 — Brier Score 누적:**

각 진입 시점에서의 P_true와 실제 결과 O ∈ {0, 1}:

$$BS_{cumulative} = \frac{1}{N_{trades}}\sum_{n=1}^{N_{trades}}(P_{true,n} - O_n)^2$$

Phase 1 Step 1.5에서 측정한 검증 Brier Score 및 Pinnacle 기준선과 비교.

**3. Edge 실현율:**

$$\text{Edge Realization} = \frac{\text{실제 평균 수익률}}{\text{예상 평균 } EV_{adj}}$$

1.0에 가까우면 모델이 정확, 0.5 미만이면 모델 과신(Overconfidence) 의심.

**4. 슬리피지 실적:**

$$\text{Avg Slippage} = \frac{1}{N}\sum_{n} (\text{Fill Price}_n - \text{Best Quote at Signal}_n)$$

슬리피지가 평균 엣지의 50%를 초과하면 실행 전략 재검토.

**5. 쿨다운 효과 분석:**

쿨다운 중 억제된 주문들의 가상 P&L을 역산하여,
쿨다운이 실제로 역선택 손실을 방지했는지 검증.

**6. ob_freeze 효과 분석:**

ob_freeze 중 억제된 주문들의 가상 P&L을 역산하여,
ob_freeze가 역선택 손실을 방지했는지 검증.

#### 신규 지표 (7~11) — Goalserve 풀 패키지 고유

**7. bet365 교차 검증 효과:**

```python
def analyze_bet365_validation(trades: List[TradeLog]) -> dict:
    """
    bet365 confidence별 실제 수익률 비교.
    HIGH가 LOW보다 수익이 높으면 bet365 검증이 가치 있는 것.
    """
    high = [t for t in trades if t.bet365_confidence == "HIGH"]
    low = [t for t in trades if t.bet365_confidence == "LOW"]

    return {
        "high_conf_avg_return": safe_mean([t.realized_pnl for t in high]),
        "low_conf_avg_return": safe_mean([t.realized_pnl for t in low]),
        "high_conf_win_rate": win_rate(high),
        "low_conf_win_rate": win_rate(low),
        "validation_value": (
            safe_mean([t.realized_pnl for t in high])
            - safe_mean([t.realized_pnl for t in low])
        ),
        # 양수면 bet365 검증이 가치 있음
    }
```

**8. P_true^cons 방향별 분석:**

```python
def analyze_directional_cons(trades: List[TradeLog]) -> dict:
    """
    Buy Yes vs Buy No에서 P_cons가 적절했는지 평가.
    """
    yes = [t for t in trades if t.direction == "BUY_YES"]
    no = [t for t in trades if t.direction == "BUY_NO"]

    return {
        "yes_edge_realization": safe_divide(
            actual_return(yes), expected_EV(yes)
        ),
        "no_edge_realization": safe_divide(
            actual_return(no), expected_EV(no)
        ),
        # 둘 다 0.8~1.2 범위면 건강
        # No가 >>1이면 No에서 P_cons가 너무 보수적
        # No가 <<1이면 No에서 P_cons가 덜 보수적 (위험)
    }
```

**9. Preliminary 정확도:**

```python
def analyze_preliminary_accuracy(events: List[EventLog]) -> dict:
    """
    Live Odds의 preliminary 감지가 Live Score confirmed와
    얼마나 일치하는지. rapid entry 활성화 판단의 핵심 지표.
    """
    total = count_by_state(events, PRELIMINARY_DETECTED)
    confirmed = count_confirmed_match(events)
    var_cancelled = count_var_cancelled(events)
    false_alarm = count_false_alarm(events)

    return {
        "preliminary_accuracy": confirmed / total if total else 0,
        "var_cancellation_rate": var_cancelled / total if total else 0,
        "false_alarm_rate": false_alarm / total if total else 0,
        # accuracy > 0.95 AND var_rate < 0.03이면 rapid entry 고려
    }
```

**10. Rapid Entry 가상 P&L:**

```python
def analyze_rapid_entry_hypothetical(events: List[EventLog]) -> dict:
    """
    rapid entry가 비활성 상태에서,
    '만약 confirmed 직후 진입했다면' 가상 P&L 계산.
    """
    hypothetical = []
    for event in events:
        if event.type != "goal_confirmed":
            continue
        if not event.preliminary_cache:
            continue

        P_true = event.preliminary_cache.get("P_true")
        P_kalshi = event.kalshi_snapshot_at_preliminary
        if not P_true or not P_kalshi:
            continue

        hypo_EV = compute_hypothetical_EV(P_true, P_kalshi, c)
        actual_outcome = event.settlement_result
        hypo_pnl = compute_hypothetical_pnl(P_kalshi, actual_outcome, c)

        hypothetical.append({
            "EV": hypo_EV,
            "pnl": hypo_pnl,
            "P_true": P_true,
            "P_kalshi": P_kalshi,
        })

    return {
        "hypo_total_pnl": sum(h["pnl"] for h in hypothetical),
        "hypo_win_rate": win_rate_from_pnl(hypothetical),
        "hypo_avg_EV": safe_mean([h["EV"] for h in hypothetical]),
        "sample_size": len(hypothetical),
        # 양수이고 안정적이면 rapid entry 활성화
    }
```

**11. bet365 이탈 경고 효과:**

```python
def analyze_bet365_divergence(positions: List[Position]) -> dict:
    """
    bet365 이탈 경고가 발생한 포지션의 최종 결과.
    '경고 후 청산했어야 하는가?'를 사후적으로 평가.
    """
    diverged = [p for p in positions if p.had_bet365_divergence]
    if not diverged:
        return {"sample_size": 0}

    # 보유 유지한 결과
    hold_pnls = [p.final_pnl for p in diverged]

    # 경고 시점에 청산했다면?
    exit_pnls = []
    for p in diverged:
        snap = p.divergence_snapshot
        exit_price = snap["P_kalshi_bid"]
        exit_pnl = (exit_price - p.entry_price) * p.quantity
        exit_pnl -= c * max(0, exit_price - p.entry_price) * p.quantity
        exit_pnls.append(exit_pnl)

    return {
        "hold_avg_pnl": safe_mean(hold_pnls),
        "exit_avg_pnl": safe_mean(exit_pnls),
        "should_auto_exit": safe_mean(exit_pnls) > safe_mean(hold_pnls),
        "sample_size": len(diverged),
        # should_auto_exit=True이면 트리거 4를 자동 청산으로 업그레이드
    }
```

### 모델 건강 대시보드

| 지표 | 건강 | 경고 | 위험 |
|------|------|------|------|
| 누적 Brier Score | Phase 1.5 ± 0.02 | ± 0.05 | 벗어남 |
| Edge 실현율 | 0.7 ~ 1.3 | 0.5 ~ 0.7 | < 0.5 |
| 누적 P&L 추이 | 양의 기울기 | 횡보 | 음의 기울기 |
| Max Drawdown | < 10% | 10~20% | > 20% |
| **bet365 검증 가치** | **HIGH > LOW + 2¢** | **HIGH ≈ LOW** | **HIGH < LOW** |
| **Preliminary 정확도** | **> 0.95** | **0.90~0.95** | **< 0.90** |
| **No 방향 Edge 실현율** | **0.7~1.3** | **> 1.5 (너무 보수적)** | **< 0.5 (덜 보수적)** |

### 피드백 루프 — 적응적 파라미터 조정

100+ 경기 축적 후 자동 조정:

```python
def adaptive_parameter_update(analytics: dict):
    """
    사후 분석 결과에 따른 파라미터 자동 조정.
    7개 파라미터를 데이터 기반으로 최적화.
    """
    global K_frac, z, LOW_CONFIDENCE_MULTIPLIER
    global RAPID_ENTRY_ENABLED, COOLDOWN_SECONDS
    global BET365_DIVERGENCE_AUTO_EXIT

    # 1. K_frac 조정
    er = analytics["edge_realization"]
    if er >= 0.8:
        K_frac = min(K_frac + 0.05, 0.50)
    elif er < 0.5:
        K_frac = max(K_frac - 0.10, 0.10)

    # 2. bet365 검증 kelly_multiplier 조정
    bv = analytics["bet365_validation_value"]
    if bv < 0.005:
        LOW_CONFIDENCE_MULTIPLIER = 0.75   # 검증 가치 낮음 → 차이 축소
    elif bv > 0.02:
        LOW_CONFIDENCE_MULTIPLIER = 0.3    # 검증 가치 높음 → 더 엄격

    # 3. Rapid entry 활성화 판단
    pa = analytics["preliminary_accuracy"]
    vcr = analytics["var_cancellation_rate"]
    reh = analytics["rapid_entry_hypo_total_pnl"]
    if (pa > 0.95 and vcr < 0.03 and reh > 0
        and analytics["cumulative_trades"] >= 200):
        RAPID_ENTRY_ENABLED = True

    # 4. z (보수성) 조정 — 방향별
    yes_er = analytics["yes_edge_realization"]
    no_er = analytics["no_edge_realization"]
    if no_er > 1.5:
        z = max(z - 0.2, 1.0)   # No가 너무 보수적 → z 하향
    elif no_er < 0.5:
        z = min(z + 0.2, 2.0)   # No가 덜 보수적 → z 상향

    # 5. Phase 1 재학습 트리거
    if analytics["brier_score_trend"] == "worsening_3weeks":
        trigger_phase1_recalibration()

    # 6. 쿨다운 조정
    if analytics["cooldown_suppressed_profitable_rate"] > 0.6:
        COOLDOWN_SECONDS = max(COOLDOWN_SECONDS - 2, 8)

    # 7. bet365 이탈 자동화 판단
    bd = analytics["bet365_divergence_should_auto_exit"]
    if bd and analytics["bet365_divergence_sample_size"] >= 30:
        BET365_DIVERGENCE_AUTO_EXIT = True

    log.info(f"Parameters updated: K_frac={K_frac}, z={z}, "
             f"rapid={RAPID_ENTRY_ENABLED}, "
             f"cooldown={COOLDOWN_SECONDS}s, "
             f"bet365_auto_exit={BET365_DIVERGENCE_AUTO_EXIT}")
```

### Phase 1 재학습 트리거

| 조건 | 조치 |
|------|------|
| 누적 Brier Score 악화 추세 (3주 연속) | 최신 시즌 데이터로 Phase 1 재캘리브레이션 |
| γ 부호가 역전되는 경기 빈도 증가 | γ 파라미터 재학습 |
| δ 부호가 역전되는 경기 빈도 증가 | δ 파라미터 재학습, 대칭/비대칭 재비교 |
| 새 시즌 시작 | 의무적 전체 재학습 |

### 데이터 아카이브

모든 거래 로그, 틱별 P_true 스냅샷, 호가창 스냅샷, P_bet365 스냅샷을
시계열 DB에 저장한다. 이 데이터는:

- Phase 1 재학습의 추가 학습 데이터
- 실행 전략(주문 타입, 쿨다운 길이) 최적화의 입력
- 규제 준수(Compliance)를 위한 감사 추적(Audit Trail)
- bet365 교차 검증의 장기 효과 분석

### 결과물

경기별 P&L 보고서, 모델 건강 대시보드, 적응적 파라미터 조정, Phase 1 재학습 트리거 판정.

---

## 시스템 진화 로드맵

시간에 따라 시스템이 **자가 학습하면서 점진적으로 공격적**이 되는 구조:

```
Phase 0 — 페이퍼 트레이딩 (실제 주문 없음):
│  • 모든 시그널 로깅만
│  • 가상 P&L 축적
│  • preliminary 정확도 측정
│  • bet365 교차 검증 효과 측정
│  기간: 2~4주
│
▼
Phase A — 보수적 라이브 (첫 100+ 거래):
│  • K_frac = 0.25 (Quarter Kelly)
│  • z = 1.645 (90% 보수적)
│  • bet365 검증 필수 (confidence NONE이면 진입 안 함)
│  • Rapid entry 비활성화
│  • bet365 이탈 → 로깅만
│  기간: 1~2개월
│
▼
Phase B — 적응적 라이브 (100~300 거래):
│  • K_frac → Step 4.6 분석에 따라 0.25~0.50
│  • z → 방향별 최적 z 탐색 (1.0~2.0)
│  • bet365 LOW confidence → multiplier 데이터 기반 조정
│  • Rapid entry 가상 P&L 누적 중
│  기간: 2~4개월
│
▼
Phase C — 성숙 라이브 (300+ 거래):
│  • K_frac = 0.50 (Half Kelly)
│  • Rapid entry 조건부 활성화 (정확도 > 0.95일 때)
│  • bet365 이탈 → 자동 청산 (데이터가 지지하면)
│  • 파라미터 자동 조정 루프 활성화
│  지속적 운영
│
▼
(매 시즌 시작: Phase 1 의무 재학습)
```

---

## 전체 시스템 피드백 루프

```
Phase 1 (Offline Calibration)
│  파라미터: b[], γ^H, γ^A, δ_H, δ_A, Q, XGBoost weights
│  데이터: Goalserve Fixtures/Results + Live Game Stats + Pregame Odds
│
▼
Phase 2 (Pre-Match Initialization)
│  초기화: a_H, a_A, P_grid, Q_off_normalized, C_time, T_exp
│  데이터: Goalserve Live Game Stats (라인업) + Pregame Odds (당일)
│
▼
Phase 3 (Live Trading Engine)
│  실시간: P_true(t), σ_MC(t), order_allowed, P_bet365(t)
│  3-Layer: Live Odds WS (<1초) + Kalshi WS (1~2초) + Live Score REST (3~8초)
│  preliminary → confirmed 2단계 처리
│
▼
Phase 4 (Arbitrage & Execution)
│  거래: Entry/Exit, 3자 교차 검증, 방향별 Kelly, Rapid Entry
│  P&L 실현
│
▼
Step 4.6 (Post-Match Analytics)
│  분석: 11개 지표 (원래 6 + 신규 5)
│  조정: 7개 파라미터 적응적 자동 조정
│
└──────────▶ Phase 1 재학습 (트리거 시)
                │
                └──▶ 개선된 파라미터로 다음 경기 사이클 시작
```

---

## Phase 4 파이프라인 요약

```
[Phase 3: P_true, σ_MC, order_allowed, event_state, P_bet365]
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│  Step 4.1: 호가창 동기화                                      │
│  • Kalshi WS → Bid/Ask + VWAP 실효 가격                     │
│  • Goalserve Live Odds WS → bet365 내재 확률                 │
│  • 유동성 필터 (Q_min ≥ 20 계약)                             │
│  Output: P_kalshi^buy, P_kalshi^sell, P_bet365, liquidity   │
└──────────────────┬──────────────────────────────────────────┘
                   ▼
┌─────────────────────────────────────────────────────────────┐
│  Step 4.2: Edge 판별                                         │
│  • P_cons 방향별: Yes→P-zσ, No→P+zσ                        │
│  • Fee-Adjusted EV 양방향 계산                               │
│  • bet365 3자 교차 검증 → confidence (HIGH/LOW/NONE)         │
│  • 필터: EV>θ AND order_allowed AND event_state==IDLE        │
│          AND liquidity_ok AND bet365_confidence != NONE      │
│  Output: Signal(direction, EV, P_cons, confidence)           │
└──────────────────┬──────────────────────────────────────────┘
                   │
         ┌─────────┴─────────┐
         │ Entry Signal       │ 기존 포지션
         ▼                    ▼
┌──────────────────┐  ┌──────────────────────────────────────┐
│  Step 4.3:       │  │  Step 4.4: 청산                       │
│  사이징           │  │                                      │
│                  │  │  트리거 1: Edge 소멸 (EV < 0.5¢)      │
│  • 방향별 Kelly   │  │  트리거 2: Edge 역전                  │
│  • K_frac        │  │  트리거 3: 만기 평가 (종료 3분전)      │
│    (0.25~0.50)   │  │  트리거 4: bet365 이탈 경고            │
│  • bet365        │  │           → 로깅 → 데이터 축적 후     │
│    kelly_mult    │  │             자동 청산으로 업그레이드    │
│  • 경기별 한도   │  │                                      │
│    pro-rata      │  │                                      │
│  • 3-Layer 리스크│  │                                      │
│    (3%/5%/20%)   │  │                                      │
└────────┬─────────┘  └──────────┬───────────────────────────┘
         │                       │
         └───────────┬───────────┘
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  Step 4.5: 주문 실행                                         │
│                                                             │
│  • 일반 진입: Limit Order (Ask + 0~1¢)                      │
│  • 긴급 청산: Limit Order (Bid - 1¢)                        │
│  • Rapid Entry: Ask + 1¢ (조건부, 데이터 축적 후)            │
│  • 부분 체결: 5초 타이머 → 미체결분 취소                      │
│                                                             │
│  거래 로그: P_true, P_cons, P_kalshi, P_bet365,             │
│            bet365_confidence, event_state, 전 필드           │
│                                                             │
│  실시간 피드백: 포지션 DB + Bankroll + 리스크 한도 갱신       │
└──────────────────┬──────────────────────────────────────────┘
                   ▼ (경기 종료 후)
┌─────────────────────────────────────────────────────────────┐
│  Step 4.6: 정산 & 사후 분석                                   │
│                                                             │
│  원래 지표 (1~6):                                           │
│  1. 경기별 P&L                                              │
│  2. Brier Score (vs Pinnacle 기준선)                        │
│  3. Edge 실현율                                             │
│  4. 슬리피지 실적                                           │
│  5. 쿨다운 효과                                             │
│  6. ob_freeze 효과                                          │
│                                                             │
│  신규 지표 (7~11):                                          │
│  7. bet365 교차 검증 효과 (HIGH vs LOW 수익 비교)            │
│  8. P_cons 방향별 분석 (Yes vs No Edge 실현율)               │
│  9. Preliminary 정확도 (rapid entry 판단용)                  │
│ 10. Rapid Entry 가상 P&L (활성화 전 시뮬레이션)              │
│ 11. bet365 이탈 경고 효과 (자동 청산 업그레이드 판단)         │
│                                                             │
│  적응적 파라미터 조정 (7개):                                 │
│  1. K_frac (0.25~0.50)                                     │
│  2. bet365 LOW kelly_multiplier                            │
│  3. Rapid entry on/off                                     │
│  4. z (보수성, 방향별)                                      │
│  5. Phase 1 재학습 트리거                                   │
│  6. 쿨다운 길이 (15초~8초)                                  │
│  7. bet365 이탈 자동 청산 on/off                            │
│                                                             │
│  시스템 진화: Phase 0 → A → B → C 로드맵                    │
│                                                             │
│  Output: P&L 보고서, 건강 대시보드, 파라미터 조정, 재학습     │
└─────────────────────────────────────────────────────────────┘
              │
              ▼
   [Phase 1 재학습 (트리거 시)]
```
