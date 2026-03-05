# Phase 4: Arbitrage & Execution — Goalserve Full Package (v2)

> **v1 → v2 변경 이력:**
>
> | # | 심각도 | 수정 내용 |
> |---|--------|----------|
> | 1 | 🔴 치명 | Edge Reversal Buy No 임계값: `(1-P_kalshi_bid)` → `P_kalshi_bid` |
> | 2 | 🔴 치명 | Expiry Eval: Buy No용 E_hold 공식 분기 추가 |
> | 3 | 🟠 중요 | bet365 Divergence Buy No 임계값: `(1-entry)` → `entry` |
> | 4 | 🟠 중요 | bet365 "독립 검증" → "시장 정합성 확인"으로 하향, multiplier 1.0→0.8 |
> | 5 | 🟠 중요 | EV/Kelly에 VWAP 실효 가격 연결 (2-pass 계산) |
> | 6 | 🟠 중요 | Paper 체결에 VWAP + 슬리피지 + 부분 체결 시뮬레이션 |
> | 7 | 🟡 보통 | Rapid Entry에 VAR 안전 대기 + P_cons 보정 + 조건 강화 |
> | 8 | 🔴 치명 | 자동 정산 P&L: Buy No 방향별 공식 분기 |

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

다만 공격성은 Phase 0 → A → B → C 로드맵에 따라 **점진적으로** 높인다.

### Buy Yes vs Buy No — 확률 공간 규약

본 문서에서 모든 확률과 가격은 **Yes 확률 공간**에서 표현한다:

| 기호 | 의미 | 공간 |
|------|------|------|
| P_true | 모델이 산출한 이벤트 발생 확률 | Yes 확률 |
| P_kalshi_buy | Kalshi Best Ask (Yes 매수 가격) | Yes 확률 |
| P_kalshi_sell | Kalshi Best Bid (Yes 매도 가격) | Yes 확률 |
| P_bet365 | bet365 내재 확률 | Yes 확률 |
| entry_price | 진입 시 체결 가격 | Yes 확률 |
| settlement | 만기 정산 가격 (100¢ or 0¢) | Yes 확률 |

**Buy No 포지션:**
- "Yes를 sell"한 것 → entry_price는 Yes를 판 가격
- No 승리(= Yes 0¢ 정산) → 수익 = entry_price
- No 패배(= Yes 100¢ 정산) → 손실 = (1 - entry_price)

> **v2 핵심 원칙:** Buy No의 모든 수식에서 `(1 - P)` 변환을 하지 않는다.
> P_cons, P_kalshi, P_bet365, entry_price 모두 Yes 공간에서 그대로 비교한다.
> 변환은 승/패 시 payoff 계산에서만 방향별로 분기한다.

---

## Input Data

**Phase 3 산출물 (매 1초):**

| 항목 | 설명 | 업데이트 빈도 |
|------|------|-------------|
| P_true(t) | 각 시장별 진짜 확률 (Yes 공간) | 매 1초 |
| σ_MC(t) | Monte Carlo 표준오차 (해석적: 0) | 매 1초 |
| order_allowed | NOT cooldown AND NOT ob_freeze AND event_state == IDLE | 매 1초 + 이벤트 시 |
| event_state | IDLE / PRELIMINARY / CONFIRMED | 이벤트 시 |
| pricing_mode | Analytical / Monte Carlo | 이벤트 시 전환 |
| engine_phase | FIRST_HALF / HALFTIME / SECOND_HALF / FINISHED | 피리어드 변경 시 |
| μ_H, μ_A | 잔여 기대 득점 | 매 1초 (로깅용) |
| **P_bet365(t)** | **bet365 인플레이 내재 확률 (Yes 공간)** | **매 Push (<1초)** |

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
Goalserve Live Odds의 bet365 배당을 시장 참조 가격으로 추가 활용한다.

### Kalshi 호가 수신

**Bid/Ask 분리:**

$$P_{kalshi}^{buy} = \frac{\text{Best Ask (¢)}}{100}, \quad P_{kalshi}^{sell} = \frac{\text{Best Bid (¢)}}{100}$$

**호가창 깊이(Depth) — VWAP 실효 가격:**

> **[v2 수정 #5] VWAP를 Step 4.2의 EV 계산에 직접 연결한다.**

주문 수량 Q에 대해 가중 평균 실효 가격을 계산한다:

$$P_{effective}(Q) = \frac{\sum_{level} p_{level} \times q_{level}}{\sum_{level} q_{level}} \quad \text{(Q 계약까지 누적)}$$

```python
class OrderBookSync:
    def __init__(self):
        # Kalshi 호가
        self.kalshi_best_bid = None
        self.kalshi_best_ask = None
        self.kalshi_depth_ask = []  # [(price, qty), ...] 오름차순
        self.kalshi_depth_bid = []  # [(price, qty), ...] 내림차순

        # bet365 참조 가격
        self.bet365_implied = {}

    def compute_vwap_buy(self, target_qty: int) -> Optional[float]:
        """
        target_qty 계약을 매수할 때의 실효 가격 (VWAP).
        Ask 호가를 낮은 가격부터 소비.
        호가 부족 시 None 반환.
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
            return None  # 호가 부족

        return cost / filled

    def compute_vwap_sell(self, target_qty: int) -> Optional[float]:
        """target_qty 계약을 매도할 때의 실효 가격 (Bid VWAP)."""
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
        """Goalserve Live Odds WebSocket → bet365 내재 확률 변환"""
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

### 유동성 필터

호가창 총 물량이 최소 기준 미달이면 진입하지 않는다:

$$\text{Total Ask Depth} \geq Q_{min} \quad (\text{예: } Q_{min} = 20\text{ 계약})$$

### bet365 참조 가격 — "시장 정합성 확인" (독립 검증이 아님)

> **[v2 수정 #4] bet365를 "독립적 검증자"가 아닌 "시장 정합성 확인"으로 재분류한다.**
>
> 이유: P_true와 P_bet365가 **같은 Goalserve 피드에서 유래**하므로,
> 특히 이벤트 직후에는 "같은 정보를 본 결과"일 수 있다.
> 진정한 독립 검증이 아니라 "시장 방향 정합성 확인"으로 하향 조정하고,
> kelly_multiplier를 1.0이 아닌 0.8로 설정한다.
>
> **bet365가 가치 있는 구간:**
> - 이벤트 미발생 시 (정상 틱): 모델은 MMPP 시간 감쇠, bet365는 자체 마켓 메이킹
>   → 정보 기반이 다름 → 정합성 확인 가치 있음
> - 이벤트 발생 직후: 같은 Goalserve 이벤트에서 유래
>   → 독립성 낮음 → 정합성 확인 가치 낮음

**bet365 참조 가격의 3가지 용도:**

| 용도 | 방법 | Step |
|------|------|------|
| **시장 정합성 확인** | 모델 방향과 bet365 방향 비교 | Step 4.2 |
| **사이징 조정** | 정합 시 0.8x, 불일치 시 0.5x | Step 4.3 |
| **청산 판단 보조** | bet365가 포지션과 반대 방향이면 경고 | Step 4.4 |

### 결과물

| 항목 | 설명 |
|------|------|
| $P_{kalshi}^{buy}(t)$ | Yes 매수 시 best ask (1호가) |
| $P_{kalshi}^{sell}(t)$ | Yes 매도 시 best bid (1호가) |
| $P_{effective}^{buy}(Q)$ | Q 계약 매수 시 VWAP 실효 가격 |
| $P_{effective}^{sell}(Q)$ | Q 계약 매도 시 VWAP 실효 가격 |
| $P_{bet365}(t)$ | bet365 인플레이 내재 확률 (시장별) |
| liquidity_ok | 유동성 필터 통과 여부 |
| depth_profile | 호가 레벨별 수량 |

---

## Step 4.2: Fee-Adjusted Edge 판별 (EV 산출)

### 목표

모델의 P_true와 시장의 P_kalshi를 비교하여,
수수료와 슬리피지를 감안하고도 양의 기댓값(Positive EV)이 있는지 검증한다.
bet365를 시장 정합성 확인으로 추가하여 Edge의 신뢰도를 분류한다.

### P_true^cons — 방향별 보수적 보정

```python
def compute_conservative_P(P_true: float, sigma_MC: float,
                            direction: str, z: float = 1.645) -> float:
    """
    Buy Yes: P가 높을수록 유리 → 하한 사용 (보수적으로 낮춤)
    Buy No:  P가 낮을수록 유리 → 상한 사용 (보수적으로 높임)
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

### Fee-Adjusted EV — 2-Pass VWAP 연결

> **[v2 수정 #5] EV 계산에 best ask/bid 대신 VWAP 실효 가격을 사용한다.**
>
> 순환 의존성 해소: EV → Kelly → 수량 → VWAP → EV
> 해법: 2-pass 계산.

```python
def compute_signal_with_vwap(
    P_true: float, sigma_MC: float,
    ob_sync: OrderBookSync,
    c: float, z: float, K_frac: float,
    bankroll: float, market_ticker: str
) -> Signal:
    """
    2-Pass 계산으로 VWAP를 EV에 연결.
    
    Pass 1: best ask/bid로 대략적 수량 산출
    Pass 2: 대략적 수량의 VWAP로 최종 EV 산출
    """
    # ═══ Pass 1: best ask/bid 기준 대략적 평가 ═══
    P_best_ask = ob_sync.kalshi_best_ask
    P_best_bid = ob_sync.kalshi_best_bid

    # Buy Yes 방향
    P_cons_yes = P_true - z * sigma_MC
    rough_EV_yes = (
        P_cons_yes * (1 - c) * (1 - P_best_ask)
        - (1 - P_cons_yes) * P_best_ask
    )

    # Buy No 방향
    P_cons_no = P_true + z * sigma_MC
    rough_EV_no = (
        (1 - P_cons_no) * (1 - c) * P_best_bid
        - P_cons_no * (1 - P_best_bid)
    )

    # 방향 결정 (더 큰 EV 쪽)
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

    # 대략적 수량 산출
    rough_f = rough_kelly(direction, P_cons, rough_P_kalshi, c, K_frac, rough_EV_yes if direction == "BUY_YES" else rough_EV_no)
    rough_qty = int(rough_f * bankroll / rough_P_kalshi)
    if rough_qty < 1:
        return Signal(direction="HOLD")

    # ═══ Pass 2: VWAP 기준 최종 EV ═══
    if direction == "BUY_YES":
        P_effective = ob_sync.compute_vwap_buy(rough_qty)
    else:
        P_effective = ob_sync.compute_vwap_sell(rough_qty)

    if P_effective is None:
        return Signal(direction="HOLD")  # 호가 부족

    # 최종 EV (VWAP 기반)
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
        return Signal(direction="HOLD")  # VWAP 반영 후 엣지 소멸

    return Signal(
        direction=direction,
        EV=final_EV,
        P_cons=P_cons,
        P_kalshi=P_effective,  # ← VWAP 실효 가격
        rough_qty=rough_qty,
        market_ticker=market_ticker
    )
```

### 시장 정합성 확인 — bet365 참조

> **[v2 수정 #4] "독립 검증" → "시장 정합성 확인"으로 재명명.**
> kelly_multiplier: ALIGNED=0.8 (1.0이 아님), DIVERGENT=0.5.

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
    모델과 bet365의 시장 방향 정합성을 확인한다.
    
    "독립적 검증"이 아닌 "시장 정합성 확인":
    - 같은 Goalserve 피드에서 유래하므로 완전한 독립성은 없음
    - 모델(MMPP)과 bet365(트레이더+알고)의 해석 차이만 캡처
    - 정합 시에도 1.0이 아닌 0.8 multiplier (과신 방지)
    """
    if P_bet365 is None:
        return MarketAlignment(
            status="UNAVAILABLE",
            kelly_multiplier=0.6  # 데이터 없으면 보수적
        )

    # 모든 비교는 Yes 확률 공간에서 수행
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
            kelly_multiplier=0.8  # [v2] 1.0이 아닌 0.8 (독립성 제한 반영)
        )
    else:
        return MarketAlignment(
            status="DIVERGENT",
            kelly_multiplier=0.5
        )
```

### 필터링 조건

| 조건 | 설명 |
|------|------|
| final_EV > θ_entry | 최소 엣지 (θ_entry = 0.02 = 2¢), **VWAP 반영 후** |
| order_allowed = True | NOT cooldown AND NOT ob_freeze |
| event_state == IDLE | preliminary 중 아님 |
| liquidity_ok = True | 호가창 최소 물량 충족 |
| engine_phase ∈ {FIRST_HALF, SECOND_HALF} | 하프타임/종료 시 진입 불가 |
| alignment.status ≠ "DIVERGENT" (초기) | 시장 불일치 시 진입 차단 (Phase A) |

> **Phase 진화에 따른 필터 완화:**
> - Phase A: DIVERGENT이면 진입 차단 (보수적)
> - Phase B: DIVERGENT이면 multiplier 0.5로 진입 허용 (Step 4.6 데이터 기반)
> - Phase C: Step 4.6에서 DIVERGENT 거래의 실적이 양수면 multiplier 조정

### 시그널 생성

```python
@dataclass
class Signal:
    direction: str              # BUY_YES, BUY_NO, HOLD
    EV: float                   # VWAP 반영 최종 EV
    P_cons: float               # 방향별 보수적 P
    P_kalshi: float             # VWAP 실효 가격
    rough_qty: int              # Pass 1에서 산출한 대략적 수량
    alignment_status: str       # ALIGNED, DIVERGENT, UNAVAILABLE
    kelly_multiplier: float     # 0.8, 0.5, 0.6
    market_ticker: str

def generate_signal(P_true, sigma_MC, ob_sync, P_bet365,
                    c, z, K_frac, bankroll, market_ticker) -> Signal:
    """2-pass VWAP + 시장 정합성 확인"""

    # 2-pass VWAP 계산
    base_signal = compute_signal_with_vwap(
        P_true, sigma_MC, ob_sync, c, z, K_frac, bankroll, market_ticker
    )

    if base_signal.direction == "HOLD":
        return base_signal

    # 시장 정합성 확인
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

### 결과물

```
Signal(direction, EV, P_cons, P_kalshi, rough_qty,
       alignment_status, kelly_multiplier, market_ticker)
```

---

## Step 4.3: 포지션 사이징 — Fee-Adjusted Kelly Criterion

### 목표

파산 확률을 0으로 만들면서 장기 복리 수익률을 극대화하는
최적의 투자 비중 f*를 계산한다.

### 방향별 Fee-Adjusted Kelly

> P_cons가 이미 방향별로 보정되어 있으므로,
> Kelly 공식도 방향에 맞는 W/L을 사용한다.

```python
def compute_kelly(signal: Signal, c: float, K_frac: float) -> float:
    """
    방향별 P_cons + 시장 정합성 multiplier 반영 Kelly.
    P_kalshi는 VWAP 실효 가격 (Step 4.2에서 산출).
    """
    P_cons = signal.P_cons
    P_kalshi = signal.P_kalshi  # VWAP 실효 가격

    if signal.direction == "BUY_YES":
        # Yes 승리 시: (1 - P_kalshi) 수익, 수수료 c 차감
        # Yes 패배 시: P_kalshi 손실
        W = (1 - c) * (1 - P_kalshi)
        L = P_kalshi

    elif signal.direction == "BUY_NO":
        # No 승리 시 (Yes=0): P_kalshi(= Yes sell 가격) 수익, 수수료 c 차감
        # No 패배 시 (Yes=100): (1 - P_kalshi) 손실
        W = (1 - c) * P_kalshi
        L = (1 - P_kalshi)

    else:
        return 0.0

    if W * L <= 0:
        return 0.0

    f_kelly = signal.EV / (W * L)

    # Fractional Kelly
    f_invest = K_frac * f_kelly

    # 시장 정합성에 의한 추가 조정
    f_invest *= signal.kelly_multiplier
    # ALIGNED → 0.8 (시장과 방향 일치, but 독립성 제한)
    # DIVERGENT → 0.5 (시장과 불일치)
    # UNAVAILABLE → 0.6 (bet365 데이터 없음)

    return max(0.0, f_invest)
```

### Fractional Kelly 정책

| K_frac | 성장률 (Full 대비) | 변동성 (Full 대비) | 권장 상황 |
|--------|-------------------|-------------------|----------|
| 0.50 | 75% | 50% 감소 | Brier Score 우수, 100+ 거래 축적 후 |
| 0.25 | 44% | 75% 감소 | **초기 라이브 (권장 시작점)** |

Full Kelly(K_frac = 1.0)는 **사용하지 않는다**.

### 동일 경기 내 상관 포지션 제한

$$\sum_{\text{markets in match}} |f_{invest,i}| \leq f_{match\_cap}$$

초과 시 비례 축소:

$$f_{invest,i}^{scaled} = f_{invest,i} \times \frac{f_{match\_cap}}{\sum_i |f_{invest,i}|}$$

### 3-Layer 리스크 한도

```python
def apply_risk_limits(f_invest: float, match_id: str,
                      bankroll: float) -> float:
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
| 1 | f_order_cap | 0.03 (3%) | 단일 주문 자본 3% 초과 불가 |
| 2 | f_match_cap | 0.05 (5%) | 경기별 자본 5% 초과 불가 |
| 3 | f_total_cap | 0.20 (20%) | 전체 포트폴리오 20% 초과 불가 |

### 최종 투입 금액

$$\text{Amount}_i = \text{apply\_risk\_limits}(f_{invest,i}^{scaled}, \text{match\_id}, \text{Bankroll})$$

$$\text{Contracts}_i = \left\lfloor \frac{\text{Amount}_i}{P_{kalshi,i}} \right\rfloor$$

---

## Step 4.4: 포지션 청산 로직 (Exit Signal)

### 목표

경기 중 상황 변화로 엣지가 사라지거나 역전되면 기존 포지션을 닫는다.

### 청산 트리거 — 4개

#### 트리거 1: Edge 소멸

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

#### 트리거 2: Edge 역전

> **[v2 수정 #1] Buy No의 임계값: `(1 - P_kalshi_bid)` → `P_kalshi_bid`**

```python
def check_edge_reversal(position, P_true, sigma_MC, P_kalshi_bid, z):
    """
    모델이 시장보다 반대 방향으로 평가하면 즉시 청산.
    
    모든 비교는 Yes 확률 공간에서 수행.
    Buy No에서도 (1 - P) 변환 없이 그대로 비교.
    """
    if position.direction == "BUY_YES":
        P_cons = P_true - z * sigma_MC
        # 모델 P(Yes)가 시장 P(Yes)보다 θ 이상 낮으면 역전
        if P_cons < P_kalshi_bid - THETA_ENTRY:
            return ExitSignal(reason="EDGE_REVERSAL")

    elif position.direction == "BUY_NO":
        P_cons = P_true + z * sigma_MC
        # [v2 수정] 모델 P(Yes)가 시장 P(Yes)보다 θ 이상 높으면
        # → P(No)가 시장보다 낮음 → No 포지션 역전
        if P_cons > P_kalshi_bid + THETA_ENTRY:
            return ExitSignal(reason="EDGE_REVERSAL")
        # ❌ 이전: if P_cons > (1 - P_kalshi_bid) + THETA_ENTRY
        # 이전 코드는 bid=0.40일 때 0.62를 요구 → ~20pp 과다

    return None
```

> **수정 검증:**
> Buy No, P_kalshi_bid = 0.40, θ = 0.02
> - ❌ v1: P_cons > (1 - 0.40) + 0.02 = 0.62 → 모델이 62%여야 역전 감지
> - ✅ v2: P_cons > 0.40 + 0.02 = 0.42 → 모델이 42%면 역전 감지
>
> Buy No는 "P(Yes)가 낮다"에 베팅한 것이므로,
> P_cons(Yes)가 시장가+θ를 초과하면 역전이 맞다.

#### 트리거 3: 시간 기반 만기 평가 (종료 3분 전)

> **[v2 수정 #2] Buy No용 E_hold 공식 분기 추가.**

```python
def check_expiry_eval(position, P_true, sigma_MC, P_kalshi_bid, c, z, t, T):
    """
    경기 종료 직전: 만기 보유 vs 현재가 청산 비교.
    방향별로 E_hold 공식이 다르다.
    """
    if T - t >= 3:
        return None

    if position.direction == "BUY_YES":
        P_cons = P_true - z * sigma_MC
    else:
        P_cons = P_true + z * sigma_MC

    # ─── E_hold: 만기까지 보유할 때의 기대값 ───
    if position.direction == "BUY_YES":
        # Yes 승리(확률 P_cons): 수익 = (1 - entry) × (1-c)
        # Yes 패배(확률 1-P_cons): 손실 = entry
        E_hold = (
            P_cons * (1 - c) * (1 - position.entry_price)
            - (1 - P_cons) * position.entry_price
        )

    elif position.direction == "BUY_NO":
        # [v2 수정] No 승리(확률 1-P_cons): 수익 = entry × (1-c)
        # No 패배(확률 P_cons): 손실 = (1 - entry)
        E_hold = (
            (1 - P_cons) * (1 - c) * position.entry_price
            - P_cons * (1 - position.entry_price)
        )
        # ❌ v1: Buy Yes 공식을 그대로 사용 → No 포지션의 기대값이 뒤집힘

    # ─── E_exit: 지금 청산할 때의 기대값 ───
    if position.direction == "BUY_YES":
        # Yes를 bid에 매도
        profit_if_exit = P_kalshi_bid - position.entry_price
    elif position.direction == "BUY_NO":
        # No를 청산 = Yes를 bid에 매수하여 포지션 닫기
        # No 진입 시 entry_price에 Yes를 sell → 청산 시 P_kalshi_bid에 Yes를 buy
        profit_if_exit = position.entry_price - P_kalshi_bid

    fee_if_exit = c * max(0, profit_if_exit)
    E_exit = profit_if_exit - fee_if_exit

    if E_exit > E_hold:
        return ExitSignal(reason="EXPIRY_EVAL", E_hold=E_hold, E_exit=E_exit)
    return None
```

> **수정 검증 (Buy No):**
> entry=0.40, P_cons=0.35, c=0.07
>
> E_hold = (1-0.35) × (1-0.07) × 0.40 - 0.35 × (1-0.40)
>        = 0.65 × 0.93 × 0.40 - 0.35 × 0.60
>        = 0.2418 - 0.21 = +0.0318 (보유 유리)
>
> ❌ v1 (Buy Yes 공식 적용):
> E_hold = 0.35 × 0.93 × 0.60 - 0.65 × 0.40
>        = 0.1953 - 0.26 = -0.0647 (보유 불리 → 잘못 청산!)

#### 트리거 4: bet365 이탈 경고

> **[v2 수정 #3] Buy No 임계값: `(1 - entry_price)` → `entry_price`**

```python
def check_bet365_divergence(position, P_bet365: float) -> Optional[DivergenceAlert]:
    """
    보유 포지션 방향과 bet365가 반대로 움직이면 경고.
    모든 비교는 Yes 확률 공간에서 수행.
    """
    if P_bet365 is None:
        return None

    DIVERGENCE_THRESHOLD = 0.05  # 5pp

    if position.direction == "BUY_YES":
        # Yes 보유: bet365 P(Yes)가 entry보다 5pp 이상 하락하면 경고
        if P_bet365 < position.entry_price - DIVERGENCE_THRESHOLD:
            return DivergenceAlert(
                severity="WARNING",
                P_bet365=P_bet365,
                P_entry=position.entry_price,
                suggested_action="REDUCE_OR_EXIT"
            )

    elif position.direction == "BUY_NO":
        # [v2 수정] No 보유(= Yes를 sell): 
        # bet365 P(Yes)가 entry보다 5pp 이상 상승하면 경고
        # (bet365가 Yes 방향으로 가면 우리 No 포지션이 불리)
        if P_bet365 > position.entry_price + DIVERGENCE_THRESHOLD:
            return DivergenceAlert(
                severity="WARNING",
                P_bet365=P_bet365,
                P_entry=position.entry_price,
                suggested_action="REDUCE_OR_EXIT"
            )
        # ❌ v1: if P_bet365 > (1 - position.entry_price) + 0.05
        # entry=0.40일 때 v1은 0.65 필요 (25pp), v2는 0.45 필요 (5pp)

    return None
```

> **수정 검증 (Buy No):**
> entry=0.40 (Yes를 0.40에 sell)
> - ❌ v1: P_bet365 > (1-0.40)+0.05 = 0.65 → 25pp 이동 필요
> - ✅ v2: P_bet365 > 0.40+0.05 = 0.45 → 5pp 이동으로 경고 (Buy Yes와 대칭)

**트리거 4는 초기에 로깅만.** Step 4.6에서 데이터 축적 후 자동 청산 여부를 결정.

### 전체 청산 평가 루프

```python
async def evaluate_exit(position, P_true, sigma_MC, P_kalshi_bid,
                        P_bet365, c, z, t, T) -> Optional[ExitSignal]:
    """매 틱에서 모든 보유 포지션에 대해 호출"""

    # 트리거 1: Edge 소멸
    exit = check_edge_decay(position, P_true, sigma_MC, P_kalshi_bid, c, z)
    if exit: return exit

    # 트리거 2: Edge 역전
    exit = check_edge_reversal(position, P_true, sigma_MC, P_kalshi_bid, z)
    if exit: return exit

    # 트리거 3: 만기 평가
    exit = check_expiry_eval(position, P_true, sigma_MC, P_kalshi_bid, c, z, t, T)
    if exit: return exit

    # 트리거 4: bet365 이탈 경고
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

## Step 4.5: 주문 실행 및 리스크 관리 (Execution & Risk Management)

### 주문 타입

| 상황 | 주문 타입 | 이유 |
|------|----------|------|
| 일반 진입 | Limit Order (Ask + 0~1¢) | 체결 확률과 슬리피지의 균형 |
| 긴급 청산 | Limit Order (Bid - 1¢) | 빠른 체결 우선 |
| **Rapid Entry** | **Limit Order (Ask + 1¢)** | **이벤트 직후 정보 우위 (조건부)** |
| 유동성 부족 | 주문 보류 | 슬리피지 > Edge이면 진입 불가 |

### 주문 제출

```python
async def execute_order(signal: Signal, amount: float,
                        ob_sync: OrderBookSync,
                        urgent: bool = False) -> Optional[FillResult]:
    P_kalshi = signal.P_kalshi  # VWAP 실효 가격
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

### Paper 체결 시뮬레이션

> **[v2 수정 #6] VWAP + 슬리피지 + 부분 체결 시뮬레이션.**

```python
class PaperExecutionLayer:
    def __init__(self, slippage_ticks: int = 1):
        self.slippage_ticks = slippage_ticks

    async def execute_order(self, signal: Signal, amount: float,
                            ob_sync: OrderBookSync,
                            urgent: bool = False) -> Optional[PaperFill]:
        """
        Paper 체결 시뮬레이션:
        1. VWAP 기반 체결가 (호가 깊이 반영)
        2. 슬리피지 가산 (1~2 tick)
        3. 부분 체결 시뮬레이션 (호가 물량 초과 시)
        
        ❌ v1: best ask에 즉시 전량 체결 → 낙관적 편향
        ✅ v2: 현실적 체결 시뮬레이션
        """
        target_qty = int(amount / signal.P_kalshi)
        if target_qty < 1:
            return None

        # VWAP 기반 실효 가격
        if signal.direction == "BUY_YES":
            P_effective = ob_sync.compute_vwap_buy(target_qty)
        else:
            P_effective = ob_sync.compute_vwap_sell(target_qty)

        if P_effective is None:
            return None  # 호가 부족

        # 슬리피지 가산
        fill_price = P_effective + (self.slippage_ticks * 0.01)

        # 부분 체결: 호가 물량 기반
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

> **[v2 수정 #7] VAR 안전 대기 + P_cons 보정 + 활성화 조건 강화.**

```python
async def post_event_rapid_entry(model, confirmed_event):
    """
    이벤트 확정 직후, 쿨다운 전에 즉시 진입 가능 여부 평가.
    """
    if not RAPID_ENTRY_ENABLED:
        return

    # [v2] VAR 안전 대기: CONFIRMED 후 추가 N초 대기
    # 이 시간 동안 Live Odds에서 score rollback이 없으면 안전
    await asyncio.sleep(VAR_SAFETY_WAIT)  # 기본 5초

    # 대기 후 상태 확인
    if model.event_state != "IDLE":
        return  # 새 이벤트 발생 — 중단
    if model.S != confirmed_event.score:
        return  # 스코어 변경됨 — VAR 취소 가능성

    # 사전 계산된 P_true 사용
    if not model.preliminary_cache.get("μ_H"):
        return

    P_true = compute_P_from_preliminary(model)
    sigma_MC = model.preliminary_cache.get("sigma_MC", 0.01)

    # [v2] P_cons 보수적 보정 (v1은 P_cons=P_true로 보정 없었음)
    direction = infer_direction(P_true, model.ob_sync.kalshi_best_ask)
    P_cons = compute_conservative_P(P_true, sigma_MC, direction, model.config.z)

    P_bet365 = model.ob_sync.bet365_implied.get(market_key)
    P_kalshi = model.ob_sync.kalshi_best_ask

    if P_bet365 is None or P_kalshi is None:
        return

    # 시장 정합성 확인
    alignment = check_market_alignment(P_cons, P_kalshi, P_bet365, direction)

    if alignment.status == "ALIGNED":
        # VWAP 기반 EV 계산
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

**Rapid Entry 활성화 조건 (강화):**

```python
RAPID_ENTRY_ENABLED = (
    cumulative_trades >= 200
    and edge_realization >= 0.8
    and preliminary_accuracy >= 0.95
    and var_cancellation_rate < 0.03
    and VAR_SAFETY_WAIT >= 5              # [v2] 안전 대기 시간 설정됨
    and rapid_entry_hypo_pnl_after_slip > 0  # [v2] 슬리피지 반영 후에도 양수
)
```

### 거래 로그

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
    P_true_cons_at_order: float     # 방향별 보수적 P
    P_kalshi_at_order: float        # VWAP 실효 가격
    P_kalshi_best_at_order: float   # best ask/bid (VWAP와 비교용)
    P_bet365_at_order: float
    EV_adj: float                   # VWAP 반영 최종 EV
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
    paper_slippage: float           # Paper 모드: 시뮬레이션 슬리피지
```

---

## Step 4.6: 경기 종료 정산 및 사후 분석

### 자동 정산

> **[v2 수정 #8] Buy No 방향별 정산 공식 분기.**

```python
def compute_realized_pnl(position, settlement_price: float,
                          fee_rate: float) -> float:
    """
    방향별 정산 P&L 계산.
    
    settlement_price: Yes 관점 정산가 (Yes 승리=1.00, Yes 패배=0.00)
    
    Buy Yes: 이익 = Settlement - Entry (Yes가 100¢이면 수익)
    Buy No:  이익 = Entry - Settlement (Yes가 0¢이면 수익)
    
    ❌ v1: Qty × (Settlement - Entry) - Fee → Buy No에서 부호 역전
    ✅ v2: 방향별 분기
    """
    if position.direction == "BUY_YES":
        gross_pnl = (settlement_price - position.entry_price) * position.quantity
    elif position.direction == "BUY_NO":
        gross_pnl = (position.entry_price - settlement_price) * position.quantity
    else:
        gross_pnl = 0

    # 수수료: 수익에 대해서만 부과
    fee = fee_rate * max(0, gross_pnl)
    return gross_pnl - fee
```

> **수정 검증:**
>
> | 방향 | Entry | Settlement | v1 결과 | v2 결과 | 실제 |
> |------|-------|------------|---------|---------|------|
> | Buy Yes | 0.45 | 1.00 | +0.55 ✅ | +0.55 ✅ | 수익 |
> | Buy Yes | 0.45 | 0.00 | -0.45 ✅ | -0.45 ✅ | 손실 |
> | Buy No | 0.40 | 0.00 | -0.40 ❌ | +0.40 ✅ | 수익 (No 승리) |
> | Buy No | 0.40 | 1.00 | +0.60 ❌ | -0.60 ✅ | 손실 (No 패배) |
>
> v1은 Buy No에서 수익과 손실이 완전히 뒤집힌다. 이건 정산 P&L이 체계적으로 잘못 기록되어
> Step 4.6의 모든 사후 분석 지표(Brier Score, Edge 실현율, Drawdown 등)가 오염된다.

### 사후 분석 지표 — 11개

#### 원래 지표 (1~6)

**1. 경기별 P&L:**

$$\text{Match P\&L} = \sum_{i \in \text{positions}} \text{compute\_realized\_pnl}(i)$$

**2. Brier Score 누적** (vs Pinnacle 기준선)

**3. Edge 실현율:**

$$\text{Edge Realization} = \frac{\text{실제 평균 수익률}}{\text{예상 평균 } EV_{adj}}$$

**4. 슬리피지 실적:**

$$\text{Avg Slippage} = \frac{1}{N}\sum_{n} (\text{Fill Price}_n - P_{kalshi,best,n})$$

> P_kalshi_best_at_order(best ask/bid)와 실제 체결가의 차이.
> VWAP가 EV에 반영되었으므로, 슬리피지 = VWAP와 실제 체결가의 차이도 추가로 추적.

**5. 쿨다운 효과 분석**

**6. ob_freeze 효과 분석**

#### 신규 지표 (7~11)

**7. 시장 정합성 효과:**

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

**8. P_true^cons 방향별 분석:**

```python
def analyze_directional_cons(trades):
    yes = [t for t in trades if t.direction == "BUY_YES"]
    no = [t for t in trades if t.direction == "BUY_NO"]

    return {
        "yes_edge_realization": safe_divide(actual_return(yes), expected_EV(yes)),
        "no_edge_realization": safe_divide(actual_return(no), expected_EV(no)),
    }
```

**9. Preliminary 정확도**

**10. Rapid Entry 가상 P&L:**

> [v2] 가상 P&L에도 VWAP + 슬리피지를 반영하여 현실적 추정.

**11. bet365 이탈 경고 효과**

### 모델 건강 대시보드

| 지표 | 건강 🟢 | 경고 🟡 | 위험 🔴 |
|------|---------|---------|---------|
| Brier Score | Phase 1.5 ± 0.02 | ± 0.05 | 벗어남 |
| Edge 실현율 | 0.7~1.3 | 0.5~0.7 | < 0.5 |
| Max Drawdown | < 10% | 10~20% | > 20% |
| 시장 정합성 가치 | ALIGNED > DIVERGENT + 1¢ | 차이 ≈ 0 | ALIGNED < DIVERGENT |
| Preliminary 정확도 | > 0.95 | 0.90~0.95 | < 0.90 |
| No 방향 실현율 | 0.7~1.3 | > 1.5 (너무 보수적) | < 0.5 |

### 피드백 루프 — 적응적 파라미터 조정

```python
def adaptive_parameter_update(analytics: dict):
    """7개 파라미터 데이터 기반 자동 조정"""

    # 1. K_frac 조정
    er = analytics["edge_realization"]
    if er >= 0.8:
        K_frac = min(K_frac + 0.05, 0.50)
    elif er < 0.5:
        K_frac = max(K_frac - 0.10, 0.10)

    # 2. 시장 정합성 multiplier 조정
    av = analytics["alignment_value"]
    if av < 0.005:
        # 정합성 확인 가치 낮음 → DIVERGENT multiplier 상향
        DIVERGENT_MULTIPLIER = 0.65
    elif av > 0.015:
        DIVERGENT_MULTIPLIER = 0.4

    # 3. Rapid entry 활성화 판단
    if (analytics["preliminary_accuracy"] > 0.95
        and analytics["var_cancellation_rate"] < 0.03
        and analytics["rapid_entry_hypo_pnl_after_slip"] > 0
        and analytics["cumulative_trades"] >= 200):
        RAPID_ENTRY_ENABLED = True

    # 4. z (보수성) 조정 — 방향별
    no_er = analytics["no_edge_realization"]
    if no_er > 1.5:
        z = max(z - 0.2, 1.0)
    elif no_er < 0.5:
        z = min(z + 0.2, 2.0)

    # 5. Phase 1 재학습 트리거
    if analytics["brier_score_trend"] == "worsening_3weeks":
        trigger_phase1_recalibration()

    # 6. 쿨다운 조정
    if analytics["cooldown_suppressed_profitable_rate"] > 0.6:
        COOLDOWN_SECONDS = max(COOLDOWN_SECONDS - 2, 8)

    # 7. bet365 이탈 자동화 판단
    if (analytics["bet365_divergence_should_auto_exit"]
        and analytics["bet365_divergence_sample_size"] >= 30):
        BET365_DIVERGENCE_AUTO_EXIT = True
```

---

## Phase 4 파이프라인 요약 (v2)

```
[Phase 3: P_true, σ_MC, order_allowed, event_state, P_bet365]
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│  Step 4.1: 호가창 동기화                                      │
│  • Kalshi WS → Bid/Ask + VWAP buy/sell 실효 가격            │
│  • Goalserve Live Odds WS → bet365 내재 확률                 │
│  • 유동성 필터 (Q_min ≥ 20 계약)                             │
│  Output: P_kalshi^buy, P_kalshi^sell,                       │
│          P_effective^buy(Q), P_effective^sell(Q),  [v2 VWAP] │
│          P_bet365, liquidity                                │
└──────────────────┬──────────────────────────────────────────┘
                   ▼
┌─────────────────────────────────────────────────────────────┐
│  Step 4.2: Edge 판별                                         │
│  • P_cons 방향별: Yes→P-zσ, No→P+zσ                        │
│  • 2-pass VWAP EV 계산:                             [v2]    │
│    Pass 1: best ask/bid → rough qty                         │
│    Pass 2: rough qty의 VWAP → final EV                      │
│  • 시장 정합성 확인:                                 [v2]    │
│    → ALIGNED (mult 0.8) / DIVERGENT (0.5)                   │
│       / UNAVAILABLE (0.6)                                   │
│  • 필터: EV>θ (VWAP 반영) AND order_allowed                 │
│          AND event_state==IDLE AND liquidity_ok              │
│          AND alignment (Phase별 정책)                        │
│  Output: Signal(direction, EV, P_cons,                      │
│          P_kalshi=VWAP, alignment_status)                    │
└──────────────────┬──────────────────────────────────────────┘
                   │
         ┌─────────┴─────────┐
         │ Entry Signal       │ 기존 포지션
         ▼                    ▼
┌──────────────────┐  ┌──────────────────────────────────────┐
│  Step 4.3:       │  │  Step 4.4: 청산 (방향별 수식) [v2]    │
│  사이징           │  │                                      │
│                  │  │  트리거 1: Edge 소멸 (EV < 0.5¢)      │
│  • 방향별 Kelly   │  │  트리거 2: Edge 역전                  │
│    (방향별 W/L)  │  │    Yes: P_cons < P_bid - θ           │
│  • K_frac        │  │    No:  P_cons > P_bid + θ    [v2]   │
│    (0.25~0.50)   │  │  트리거 3: 만기 평가 (종료 3분전)      │
│  • alignment     │  │    방향별 E_hold 분기          [v2]   │
│    multiplier    │  │  트리거 4: bet365 이탈 경고            │
│    (0.8/0.5/0.6) │  │    Yes: P_bet365 < entry - 5pp       │
│    [v2]          │  │    No:  P_bet365 > entry + 5pp [v2]  │
│  • 경기별 한도   │  │           → 로깅 → 데이터 축적 후     │
│    pro-rata      │  │             자동 청산으로 업그레이드    │
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
│  • Rapid Entry: Ask + 1¢ (조건부)                           │
│    + VAR 안전 대기 5초                              [v2]    │
│    + P_cons z 보정                                  [v2]    │
│  • 부분 체결: 5초 타이머 → 미체결분 취소                      │
│                                                             │
│  Paper 모드:                                        [v2]    │
│  • VWAP 기반 체결가 + 1tick 슬리피지 + 부분 체결             │
│                                                             │
│  거래 로그: P_true, P_cons, P_kalshi(VWAP),                 │
│    P_kalshi_best, P_bet365, alignment_status,       [v2]    │
│    kelly_multiplier, event_state, paper_slippage            │
│                                                             │
│  실시간 피드백: 포지션 DB + Bankroll + 리스크 한도 갱신       │
└──────────────────┬──────────────────────────────────────────┘
                   ▼ (경기 종료 후)
┌─────────────────────────────────────────────────────────────┐
│  Step 4.6: 정산 & 사후 분석                                   │
│                                                             │
│  정산 (방향별):                                      [v2]   │
│  • Buy Yes: (Settlement - Entry) × Qty - Fee               │
│  • Buy No:  (Entry - Settlement) × Qty - Fee               │
│                                                             │
│  원래 지표 (1~6):                                           │
│  1. 경기별 P&L (방향별 정산)                         [v2]   │
│  2. Brier Score (vs Pinnacle 기준선)                        │
│  3. Edge 실현율                                             │
│  4. 슬리피지 실적 (VWAP vs 실제 체결 비교 추가)      [v2]   │
│  5. 쿨다운 효과                                             │
│  6. ob_freeze 효과                                          │
│                                                             │
│  신규 지표 (7~11):                                          │
│  7. 시장 정합성 효과                                 [v2]   │
│     (ALIGNED vs DIVERGENT 수익 비교)                        │
│  8. P_cons 방향별 분석 (Yes vs No Edge 실현율)               │
│  9. Preliminary 정확도 (rapid entry 판단용)                  │
│ 10. Rapid Entry 가상 P&L (슬리피지 반영)             [v2]   │
│ 11. bet365 이탈 경고 효과 (자동 청산 업그레이드 판단)         │
│                                                             │
│  적응적 파라미터 조정 (7개):                                 │
│  1. K_frac (0.25~0.50)                                     │
│  2. DIVERGENT multiplier                             [v2]  │
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

---

## 시스템 진화 로드맵

```
Phase 0 — 페이퍼 트레이딩:
│  • K_frac = 0.25, z = 1.645
│  • Paper 체결: VWAP + 1tick 슬리피지 [v2]
│  • DIVERGENT이면 진입 차단
│  • Rapid entry 비활성화
│  기간: 2~4주
│
▼
Phase A — 보수적 라이브:
│  • DIVERGENT이면 진입 차단 유지
│  • Rapid entry 비활성화
│  기간: 1~2개월
│
▼
Phase B — 적응적 라이브:
│  • K_frac → 0.25~0.50 (Step 4.6 기반)
│  • DIVERGENT → multiplier 0.5로 진입 허용
│  • z → 방향별 최적 탐색
│  기간: 2~4개월
│
▼
Phase C — 성숙 라이브:
│  • Rapid entry 조건부 활성화 (VAR 안전 대기 포함) [v2]
│  • bet365 이탈 → 자동 청산 (데이터가 지지하면)
│  • 파라미터 자동 조정 루프 활성화
│
▼
(매 시즌: Phase 1 의무 재학습)
```

---

## 전체 시스템 피드백 루프

```
Phase 1 (Offline Calibration)
│  파라미터: b[], γ^H, γ^A, δ_H, δ_A, Q, XGBoost weights
│
▼
Phase 2 (Pre-Match Initialization)
│  초기화: a_H, a_A, P_grid, Q_off_normalized, C_time, T_exp
│
▼
Phase 3 (Live Trading Engine)
│  실시간: P_true(t), σ_MC(t), order_allowed, P_bet365(t)
│  3-Layer: Live Odds WS + Kalshi WS + Live Score REST
│
▼
Phase 4 (Arbitrage & Execution) [v2]
│  • VWAP 연결 EV (2-pass)
│  • 방향별 P_cons (Yes→하한, No→상한)
│  • 방향별 Kelly W/L
│  • 방향별 청산 트리거 (Edge 역전, 만기, 정산)
│  • 시장 정합성 확인 (독립 검증 X, multiplier 0.8)
│  • Paper: VWAP + 슬리피지 + 부분 체결
│  • Rapid Entry: VAR 안전 대기 + P_cons 보정
│
▼
Step 4.6 (Post-Match Analytics)
│  분석: 11개 지표
│  조정: 7개 파라미터
│
└──▶ Phase 1 재학습 (트리거 시)
```

---

## v2 수정 사항 추적

| # | 위치 | 변경 전 | 변경 후 |
|---|------|--------|--------|
| 1 | Step 4.4 트리거 2 | `P_cons > (1-P_bid) + θ` | `P_cons > P_bid + θ` |
| 2 | Step 4.4 트리거 3 | Buy Yes E_hold만 | 방향별 E_hold 분기 |
| 3 | Step 4.4 트리거 4 | `P_bet365 > (1-entry) + 0.05` | `P_bet365 > entry + 0.05` |
| 4 | Step 4.1~4.2 | "독립 검증", mult 1.0 | "시장 정합성", mult 0.8 |
| 5 | Step 4.2 | best ask/bid로 EV | 2-pass VWAP로 EV |
| 6 | Step 4.5 Paper | best ask 즉시 전량 체결 | VWAP + 1tick + 부분 체결 |
| 7 | Step 4.5 Rapid | VAR 대기 없음, P_cons 미보정 | 5초 대기 + z 보정 + 조건 강화 |
| 8 | Step 4.6 정산 | `Qty × (Sett - Entry)` | 방향별 `BuyYes: Sett-Entry`, `BuyNo: Entry-Sett` |