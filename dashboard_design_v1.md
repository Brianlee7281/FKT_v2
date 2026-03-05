# Dashboard Design — Kalshi 축구 퀀트 자동트레이딩 시스템

## 개요

Phase 1~4에서 흐르는 데이터 스트림을 시각화하여
시스템 운영자가 실시간 상황 판단, 리스크 모니터링, 사후 분석을 수행하는 대시보드.

페이퍼 트레이딩(Phase 0)과 라이브 트레이딩(Phase A~C) 모두 동일한 UI에서 운영하며,
모드에 따라 주문 전송 여부만 달라진다.

### 설계 원칙

1. **질문 중심 설계:** 대시보드의 모든 뷰는 운영자의 구체적 질문에 답한다
2. **점진적 구축:** 시스템 진화 Phase에 맞춰 대시보드도 점진적으로 확장
3. **3-Layer 구조:** 실시간(경기 중) → 포트폴리오(전체) → 분석(사후) 계층 분리
4. **알림 연동:** 대시보드를 항상 볼 수 없으므로 핵심 이벤트는 Push 알림

---

## 아키텍처

### 3-Layer 구조

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 1: Live Match View (경기 중 실시간)                     │
│  • 경기당 1개 패널 — 동시 10경기까지                            │
│  • 1초 단위 갱신                                              │
│  • 목적: "지금 무슨 일이 일어나고 있는가"                       │
├─────────────────────────────────────────────────────────────┤
│  Layer 2: Portfolio View (포트폴리오 전체)                      │
│  • 전 경기 통합 뷰                                            │
│  • 실시간 P&L, 노출도, 리스크 한도                             │
│  • 목적: "돈이 어디에 얼마나 묶여 있는가"                       │
├─────────────────────────────────────────────────────────────┤
│  Layer 3: Analytics View (사후 분석 + 시스템 건강)              │
│  • 경기 종료 후 + 장기 추세                                   │
│  • Step 4.6의 11개 지표 시각화                                │
│  • 목적: "시스템이 건강한가, 어디를 고쳐야 하는가"               │
└─────────────────────────────────────────────────────────────┘
```

### 데이터 흐름

```
┌────────────────────────────────────────────────────────────┐
│  Trading Engine (Python asyncio)                            │
│                                                            │
│  Phase 3 + Phase 4 — 단일 프로세스                          │
│                                                            │
│  매 1초:                                                   │
│  ├── state_snapshot → Redis Pub/Sub                        │
│  │   {t, S, X, ΔS, μ_H, μ_A, P_true, σ_MC,              │
│  │    engine_phase, event_state, cooldown, ob_freeze}      │
│  │                                                         │
│  ├── signal_snapshot → Redis Pub/Sub                       │
│  │   {EV, direction, P_cons, bet365_confidence,            │
│  │    order_allowed}                                       │
│  │                                                         │
│  └── orderbook_snapshot → Redis Pub/Sub                    │
│      {P_kalshi_buy, P_kalshi_sell, P_bet365, depth}        │
│                                                            │
│  이벤트 시:                                                │
│  └── event_log → Redis Pub/Sub + PostgreSQL                │
│      {type, source, confidence, timestamp, ...}            │
│                                                            │
│  주문 시:                                                  │
│  └── trade_log → PostgreSQL                                │
│      {TradeLog 전 필드}                                    │
│                                                            │
│  Live Odds WS (별도 코루틴):                               │
│  └── live_odds_snapshot → Redis Pub/Sub                    │
│      {score, minute, period, bet365_odds, ball_pos}        │
└──────────────────┬─────────────────────────────────────────┘
                   │
          ┌────────▼────────┐
          │     Redis        │  실시간 데이터
          │     Pub/Sub      │  (1초 미만 TTL)
          └────────┬────────┘
                   │
          ┌────────▼────────┐
          │  Dashboard       │  FastAPI + WebSocket
          │  Server          │  → Browser에 실시간 Push
          └────────┬────────┘
                   │
          ┌────────▼────────┐
          │  React Client    │  Recharts / Lightweight Charts
          │  (Browser)       │
          └─────────────────┘

          ┌─────────────────┐
          │   PostgreSQL     │  영구 저장 (Layer 3 분석용)
          │   + TimescaleDB  │
          └─────────────────┘
```

### 기술 스택

| 계층 | 기술 | 이유 |
|------|------|------|
| 실시간 메시지 | Redis Pub/Sub | 1초 이하 지연, 경량 |
| 영구 저장 | PostgreSQL + TimescaleDB | 시계열 최적화, 분석 쿼리 |
| 대시보드 서버 | FastAPI + WebSocket | Python 에코시스템 통일, 비동기 |
| 프론트엔드 | React + Recharts | 실시간 차트, 컴포넌트 재사용 |
| 배포 | 단일 서버 (초기) | 엔진 + 대시보드가 같은 머신 |

---

## 페이퍼 트레이딩 vs 라이브 트레이딩 모드

두 모드의 데이터 구조가 동일하므로, 페이퍼에서 축적한 가상 거래를
그대로 Layer 3 Analytics에서 분석할 수 있다.

```python
class TradingMode(Enum):
    PAPER = "paper"    # Phase 0: 주문 전송 안 함, 가상 체결
    LIVE = "live"      # Phase A~C: 실제 주문 전송

class ExecutionLayer:
    def __init__(self, mode: TradingMode):
        self.mode = mode

    async def execute_order(self, signal, amount, ob_sync):
        if self.mode == TradingMode.PAPER:
            # 가상 체결: 현재 호가로 즉시 체결 가정
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

| 항목 | PAPER 모드 | LIVE 모드 |
|------|-----------|----------|
| 헤더 색상 | 보라색 + "PAPER TRADING" 배지 | 표준 색상 |
| 주문 전송 | ❌ | ✅ |
| 체결 로직 | 현재 호가로 즉시 가상 체결 | Kalshi REST API 실제 제출 |
| P&L 표시 | "가상 P&L" 레이블 | 실제 P&L |
| Bankroll | 설정값 (예: $5,000) | Kalshi 실제 잔고 |
| 데이터 구조 | TradeLog 동일 (is_paper=True) | TradeLog 동일 |
| Layer 3 분석 | 동일하게 작동 | 동일하게 작동 |

---

## Layer 1: Live Match View — 경기별 실시간 패널

경기 진행 중에 가장 많이 보게 될 화면.
경기당 하나의 패널, 주말 10경기 동시 진행 시 타일 레이아웃.

### 1A: 경기 상태 헤더

```
┌─────────────────────────────────────────────────────────────┐
│  🟢 Arsenal vs Chelsea              67:23    ⚽ 1-1          │
│  EPL  │  SECOND_HALF  │  11v11  │  IDLE                    │
│  cooldown: OFF  │  ob_freeze: OFF  │  pricing: ANALYTICAL   │
└─────────────────────────────────────────────────────────────┘
```

**상태별 색상 코딩:**

| 상태 | 헤더 색상 | 의미 |
|------|----------|------|
| IDLE + 활성 | 🟢 초록 테두리 | 정상 운영, 주문 가능 |
| PRELIMINARY_DETECTED | 🟡 노란 배경 | 이벤트 감지됨, 확인 대기 |
| COOLDOWN | 🔵 파란 테두리 | 쿨다운 중, 주문 차단 |
| OB_FREEZE | 🔴 빨간 테두리 | 호가 이상, 주문 차단 |
| HALFTIME | ⚪ 회색 배경 | 하프타임 동결 |
| FINISHED | ⬛ 어두운 배경 | 경기 종료, 정산 완료 |

**데이터 소스 매핑:**

| 표시 항목 | 소스 | 갱신 주기 |
|----------|------|----------|
| 팀 이름 | Phase 2 Step 2.1 | 고정 |
| 분:초 | Live Odds WS `info.minute` | <1초 |
| 스코어 | Live Odds WS `info.score` | <1초 |
| 리그 | Phase 2 match 메타 | 고정 |
| engine_phase | Phase 3 Step 3.1 | 이벤트 시 |
| X(t) 상태 (11v11 등) | Phase 3 Step 3.3 | 레드카드 시 |
| event_state | Phase 3 Step 3.1 | 이벤트 시 |
| cooldown / ob_freeze | Phase 3 Step 3.1 | 이벤트 시 |
| pricing_mode | Phase 3 Step 3.4 | 이벤트 시 전환 |

---

### 1B: 모델 vs 시장 비교 차트 ⭐ (핵심 시각화)

**이 차트가 대시보드에서 가장 중요한 뷰.** P_true, P_kalshi, P_bet365 세 가격의
실시간 움직임을 보여준다.

```
P (확률)
0.70 ┤
     │              ╱── P_true (모델, 파랑)
0.60 ┤    ─────────╱
     │   ╱             ── P_kalshi mid (Kalshi, 빨강)
0.50 ┤──╱──────────────
     │                  ── P_bet365 (bet365, 초록)
0.40 ┤───────────────────────────────────────
     │
0.30 ┤
     └──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬── t (분)
        0  10 20 30 40  HT  55 65 75 85 90+
                        │
                    하프타임 구간
                    (회색 음영)
```

**차트 요소:**

| 요소 | 시각화 | 소스 |
|------|--------|------|
| P_true 라인 | 파랑 실선 (두꺼움) | Phase 3 Step 3.4 (매 1초) |
| P_kalshi mid 라인 | 빨강 실선 | (bid+ask)/2, Phase 4 Step 4.1 (매 1초) |
| P_kalshi bid-ask 스프레드 | 빨강 음영 (얇은 밴드) | Phase 4 Step 4.1 |
| P_bet365 라인 | 초록 점선 | Goalserve Live Odds WS (<1초) |
| 엣지 영역 | 연한 파랑 음영 | P_true > P_kalshi^ask인 구간 |
| 골 이벤트 | 수직선 + ⚽ | PRELIMINARY(점선) → CONFIRMED(실선) |
| 레드카드 | 수직선 + 🟥 | Live Score CONFIRMED |
| 하프타임 | 회색 음영 구간 | engine_phase == HALFTIME |
| 거래 마커 | 🔼(진입) 🔽(청산) | Phase 4 Step 4.5 |

**시장 탭:** Over 2.5 / Home Win / Draw / Away Win 등을 탭으로 전환.
각 탭마다 해당 시장의 P_true, P_kalshi, P_bet365를 표시.

**PRELIMINARY 시각화:**

```
     │
     │         ⚽ (점선, 노랑)     ⚽ (실선, 초록)
     │         │ PRELIMINARY      │ CONFIRMED
0.55 ┤─────────┤                  │
     │         │    ob_freeze     │  cooldown
     │         │◄──── 구간 ────►│◄── 15초 ──►│
     │         │  (노란 음영)      │ (파란 음영)  │
```

- PRELIMINARY 구간: 노란 배경 음영 + 점선 골 마커
- CONFIRMED 시: 점선 → 실선 전환 + 초록 마커
- VAR 취소 시: 점선 → 빨간 X 마커 + 노란 음영 제거

---

### 1C: 강도 함수 모니터

μ_H와 μ_A의 실시간 감쇠를 보여주는 소형 차트:

```
μ (잔여 기대 득점)
1.5 ┤╲
    │ ╲── μ_H (홈, 파랑)
1.0 ┤  ╲         ┌── 골 발생: δ 점프
    │   ──╲      │
0.5 ┤      ╲─────┤──╲── μ_A (어웨이, 빨강)
    │       ──────    ──────
0.0 ┤──────────────────────────────
    └──┬──┬──┬──┬──┬──┬──┬──┬── t
       0  15 30 45  60 75 90
```

| 이벤트 | 시각적 효과 |
|--------|-----------|
| 시간 경과 | μ가 부드럽게 감소 (theta decay) |
| 골 발생 | δ 변경으로 μ_H, μ_A가 반대 방향으로 계단 점프 |
| 레드카드 | γ^H, γ^A 변경으로 μ_H, μ_A가 반대 방향으로 계단 점프 |
| 기저함수 경계 | b_i 변경으로 μ가 약간 불연속 |

**데이터 소스:** Phase 3 Step 3.2 μ_H, μ_A (매 1초)

**이 차트의 가치:** "모델이 이벤트를 올바르게 처리하고 있는가"를
실시간으로 확인하는 유일한 방법. 골 발생 후 μ가 점프하지 않으면 버그.

---

### 1D: 트레이딩 시그널 + 포지션 패널

```
┌─────────────────────────────────────────────────────────────┐
│  Active Signals                                              │
│                                                              │
│  Over 2.5:  BUY YES │ EV: 3.2¢ │ 🟢 HIGH │ → 2 contracts  │
│  Home Win:  HOLD    │ EV: 0.8¢ │    —    │                  │
│  BTTS:      BUY NO  │ EV: 2.1¢ │ 🟡 LOW  │ → 1 contract   │
│                                                              │
│  Open Positions                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ Over 2.5 YES │ Entry: 45¢ │ Now: 52¢ │ P&L: +$1.05  │  │
│  │ Qty: 15      │ EV@entry: 3.2¢         │ bet365: 53¢ ✓│  │
│  ├────────────────────────────────────────────────────────┤  │
│  │ BTTS NO      │ Entry: 38¢ │ Now: 35¢ │ P&L: +$0.45  │  │
│  │ Qty: 8       │ EV@entry: 2.1¢         │ bet365: 36¢ ✓│  │
│  └────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

| 항목 | 소스 | 갱신 |
|------|------|------|
| 시그널 방향 + EV | Phase 4 Step 4.2 | 매 1초 |
| bet365 confidence (HIGH 🟢 / LOW 🟡) | Phase 4 Step 4.2 | 매 1초 |
| 추천 계약 수 | Phase 4 Step 4.3 | 매 1초 |
| 포지션 entry/current/P&L | Phase 4 Step 4.5 | 매 1초 |
| bet365 참조가 + ✓/⚠ | Phase 4 Step 4.1 | <1초 |

**bet365 ✓/⚠ 표시:**
- ✓ (초록): bet365가 포지션 방향과 일치
- ⚠ (노랑): bet365 이탈 경고 발생 (Step 4.4 트리거 4)

---

### 1E: 이벤트 로그 (실시간 스트림)

```
┌─────────────────────────────────────────────────────────────┐
│  Event Log                                    [Auto-scroll] │
│                                                              │
│  67:23  TICK    μ_H=0.42 μ_A=0.38 P(O2.5)=0.582           │
│  67:22  TICK    μ_H=0.43 μ_A=0.38 P(O2.5)=0.583           │
│  65:01  ORDER   BUY YES Over2.5 @45¢ ×15 (EV=3.2¢, HIGH)  │
│  65:00  SIGNAL  Over2.5 BUY_YES EV=3.2¢ bet365=HIGH        │
│  65:00  CONFIRMED  Goal (Away, Chelsea, Palmer)              │
│                    S=1-1, ΔS=0. cooldown=15s                │
│  64:55  PRELIMINARY  score 1-0→1-1 (Live Odds WS)          │
│                      ob_freeze=True, μ pre-computing...     │
│  64:54  OB_FREEZE  bet365 Δodds=15.2% → freeze             │
│  ...                                                        │
└─────────────────────────────────────────────────────────────┘
```

**이벤트 유형별 색상:**

| 유형 | 색상 | 의미 |
|------|------|------|
| PRELIMINARY | 🟡 노랑 배경 | 1차 감지 (확인 대기) |
| CONFIRMED | 🟢 초록 배경 | 확정 (상태 업데이트 완료) |
| VAR_CANCELLED | 🔴 빨강 텍스트 | VAR 취소 (상태 롤백) |
| OB_FREEZE | 🔴 빨강 배경 | 호가 이상 감지 |
| COOLDOWN | 🔵 파랑 배경 | 쿨다운 진입/종료 |
| SIGNAL | 🟣 보라 텍스트 | 트레이딩 시그널 발생 |
| ORDER | ⬛ 굵은 텍스트 | 주문 제출/체결/취소 |
| TICK | ⬜ 연한 회색 | 일반 틱 (축소 가능) |

**필터:** 이벤트 유형별 토글로 TICK을 숨기거나 특정 유형만 표시 가능.

---

### 1F: 3-Layer 감지 상태 표시기 (소형)

```
┌─────────────────────────────────────────┐
│  Data Sources                            │
│                                          │
│  Live Odds WS:  🟢 Connected  <1s       │
│  Kalshi WS:     🟢 Connected  ~1s       │
│  Live Score:    🟢 Polling    3s cycle   │
│                                          │
│  Last Events:                            │
│  • Live Odds:  score change   2s ago     │
│  • Kalshi:     ob update      0.3s ago   │
│  • Live Score: poll ok        1.2s ago   │
└─────────────────────────────────────────┘
```

| 상태 | 표시 |
|------|------|
| 정상 연결 | 🟢 |
| 지연 (>5초 미수신) | 🟡 |
| 장애 (>10초 또는 오류) | 🔴 |

---

## Layer 2: Portfolio View — 전체 포트폴리오

### 2A: 리스크 대시보드

```
┌─────────────────────────────────────────────────────────────┐
│  Portfolio Overview                                          │
│                                                              │
│  Bankroll: $5,000.00  │  Mode: 🟣 PAPER                    │
│                                                              │
│  ┌─ Risk Limits ────────────────────────────────────────┐   │
│  │                                                      │   │
│  │  L1 단일 주문 (3%):    ████░░░░░░  $87 / $150       │   │
│  │  L2 경기별 (5%):                                     │   │
│  │    ARS-CHE:            ██████░░░░  $156 / $250      │   │
│  │    LIV-MCI:            ████░░░░░░  $98 / $250       │   │
│  │    BAR-RMA:            ██░░░░░░░░  $45 / $250       │   │
│  │  L3 전체 (20%):        ██████░░░░  $412 / $1,000    │   │
│  │                                                      │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                              │
│  Summary:                                                    │
│  • Active Matches: 4                                         │
│  • Open Positions: 7                                         │
│  • Total Exposure: $412 (8.2%)                               │
│  • Unrealized P&L: +$23.50                                   │
│  • Today's Realized P&L: +$45.20                             │
└─────────────────────────────────────────────────────────────┘
```

**Progress bar 색상:**
- 0~50%: 초록
- 50~80%: 노랑
- 80%+: 빨강

**데이터 소스:**

| 항목 | 소스 | 갱신 |
|------|------|------|
| Bankroll | Kalshi REST balance (LIVE) / 설정값 (PAPER) | 주문 체결 시 |
| Layer 1/2/3 사용량 | Phase 4 Step 4.5 포지션 합산 | 주문 체결 시 |
| Active Matches | Phase 3 engine_phase 카운트 | 경기 시작/종료 시 |
| Total Exposure | 전 포지션 합산 | 매 1초 (현재가 반영) |
| Unrealized P&L | Σ(current_price - entry_price) × qty | 매 1초 |
| Realized P&L | 정산 완료된 포지션 합산 | 정산 시 |

---

### 2B: 경기별 포지션 테이블

```
┌──────────┬────────┬──────┬───────┬───────┬────────┬────────┬────────┐
│ Match    │ Market │ Dir  │ Entry │ Curr  │ P&L    │bet365  │ Status │
├──────────┼────────┼──────┼───────┼───────┼────────┼────────┼────────┤
│ ARS-CHE  │ O2.5   │ YES  │ 45¢   │ 52¢   │ +$1.05 │ 53¢ ✓  │ 67'   │
│ ARS-CHE  │ HW     │ NO   │ 62¢   │ 58¢   │ +$0.60 │ 56¢ ⚠  │ 67'   │
│ LIV-MCI  │ O2.5   │ YES  │ 55¢   │ 53¢   │ -$0.40 │ 54¢ ✓  │ 34'   │
│ BAR-RMA  │ HW     │ YES  │ 48¢   │ 51¢   │ +$0.45 │ 50¢ ✓  │ 12'   │
│ ───────  │ ────── │ ──── │ ───── │ ───── │ ────── │ ────── │ ────── │
│ JUV-NAP  │ O2.5   │ YES  │ 52¢   │ 100¢  │ +$7.20 │  —     │ FT ✅  │
│ JUV-NAP  │ BTTS   │ NO   │ 45¢   │  0¢   │ -$3.60 │  —     │ FT ❌  │
└──────────┴────────┴──────┴───────┴───────┴────────┴────────┴────────┘
                                              Net: +$5.30
```

**행 색상:**
- 미실현 수익 → 연한 초록 배경
- 미실현 손실 → 연한 빨강 배경
- 정산 완료(승) → 초록 텍스트 + ✅
- 정산 완료(패) → 빨강 텍스트 + ❌

**정렬/필터:**
- 경기별 / 시장별 / P&L순 정렬 가능
- 활성(진행 중) / 정산 완료 필터

---

### 2C: 오늘의 P&L 타임라인

```
P&L ($)
+$50 ┤                              ╱──
     │                         ╱───╱
+$30 ┤                    ╱───╱     (미실현, 점선)
     │               ╱───╱
+$10 ┤          ╱───╱
     │     ╱───╱
  $0 ┤────╱
     │   ╱
-$10 ┤──╱
     │ ╱
-$20 ┤╱
     └──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──
       12  13  14  15  16  17  18  19  20  21  시간(UTC)
```

| 라인 | 의미 | 스타일 |
|------|------|--------|
| 실현 P&L | 정산 완료된 수익 누적 | 파랑 실선 (계단형) |
| 미실현 P&L | 현재 열린 포지션의 가치 변동 | 파랑 점선 (연속) |
| 총 P&L | 실현 + 미실현 | 굵은 파랑 실선 |

---

## Layer 3: Analytics View — 사후 분석 + 시스템 건강

경기 종료 후, 또는 주간/월간 리뷰 시 보는 화면.
Step 4.6의 11개 지표를 시각화한다.

### 3A: 모델 건강 대시보드 (게이지 차트)

```
┌─────────────────────────────────────────────────────────────┐
│  System Health Dashboard                    Updated: 5m ago │
│                                                              │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  Brier Score          [■■■■■■■░░░]  0.198   🟢       │  │
│  │  ΔBS vs Pinnacle      [■■■■■■░░░░]  -0.012  🟢       │  │
│  │  Edge Realization      [■■■■■■■░░░]  0.87    🟢       │  │
│  │  Max Drawdown          [■■■░░░░░░░]  6.2%    🟢       │  │
│  │  bet365 Validation     [■■■■■■■■░░]  +2.1¢   🟢       │  │
│  │  Prelim Accuracy       [■■■■■■■■■░]  0.96    🟢       │  │
│  │  No-dir Edge Real.     [■■■■■■░░░░]  1.12    🟢       │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                              │
│  Overall Status: ✅ HEALTHY                                  │
│  Last Recalibration: 2025-08-15 (42 days ago)               │
│  Cumulative Trades: 287                                      │
│  System Phase: B (Adaptive Live)                             │
└─────────────────────────────────────────────────────────────┘
```

**7개 지표의 임계값:**

| 지표 | 건강 🟢 | 경고 🟡 | 위험 🔴 |
|------|---------|---------|---------|
| Brier Score | Phase 1.5 ± 0.02 | ± 0.05 | 벗어남 |
| ΔBS vs Pinnacle | < 0 | 0~0.02 | > 0.02 |
| Edge 실현율 | 0.7~1.3 | 0.5~0.7 | < 0.5 |
| Max Drawdown | < 10% | 10~20% | > 20% |
| bet365 검증 가치 | HIGH > LOW + 2¢ | HIGH ≈ LOW | HIGH < LOW |
| Preliminary 정확도 | > 0.95 | 0.90~0.95 | < 0.90 |
| No 방향 실현율 | 0.7~1.3 | > 1.5 (너무 보수적) | < 0.5 (위험) |

---

### 3B: Calibration Plot (Reliability Diagram)

Phase 1 Step 1.5에서 정의한 Calibration Plot을 라이브 데이터로 지속 업데이트:

```
실제 빈도
1.0 ┤                                    ╱
    │                                 ╱ /
0.8 ┤                              •╱  / (이상: 대각선)
    │                           •  ╱  /
0.6 ┤                        •   ╱  /
    │                     •    ╱  /
0.4 ┤                  •     ╱  /
    │               •      ╱  /
0.2 ┤            •       ╱  /
    │         •        ╱  /
0.0 ┤──────•─────────╱──/────────────────
    └──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──
      0.0   0.2   0.4   0.6   0.8   1.0
                    예측 확률
```

- 각 빈(bin)에 최소 20개 관측이 쌓이면 점 표시
- 대각선(회색 점선)에 가까울수록 잘 보정된 모델
- 신뢰 구간(연한 음영)은 빈 크기에 따른 이항 분포 95% CI

**시장별 탭:** 1X2 / Over/Under / BTTS 별도 Calibration Plot.

---

### 3C: 누적 P&L + Drawdown 차트

```
P&L ($)
+$600 ┤                                    ╱──────
      │                              ╱────╱
+$400 ┤                        ╱────╱      │
      │                  ╱────╱            │ ← Max DD: 6.2%
+$200 ┤            ╱────╱                 ╲│╱──
      │      ╱────╱                            ╱
   $0 ┤─────╱
      │    ╱
-$100 ┤───╱
      └──┬──┬──┬──┬──┬──┬──┬──┬──┬──┬──
        W1  W3  W5  W7  W9  W11 W13 W15
```

| 라인 | 의미 | 스타일 |
|------|------|--------|
| 실현 누적 P&L | 파랑 실선 | — |
| Drawdown 구간 | 빨강 음영 (P&L과 이전 최고점 사이) | — |
| Phase 1.5 시뮬레이션 | 초록 점선 (검증 기간 P&L) | 비교 기준 |
| Phase 전환 마커 | 수직 점선 (A→B, B→C) | — |

---

### 3D: 방향별 분석 (P_cons 진단)

```
┌────────────────────────────────────┬────────────────────────────────────┐
│  Buy Yes Direction                 │  Buy No Direction                  │
│                                    │                                    │
│  Trades: 145                       │  Trades: 87                        │
│  Win Rate: 58.6%                   │  Win Rate: 54.0%                   │
│  Edge Realization: 0.92 🟢         │  Edge Realization: 1.15 🟢         │
│                                    │                                    │
│  Avg EV at Entry: 3.1¢            │  Avg EV at Entry: 2.8¢            │
│  Avg Actual Return: 2.9¢          │  Avg Actual Return: 3.2¢          │
│                                    │                                    │
│  ┌──── EV Distribution ────┐      │  ┌──── EV Distribution ────┐      │
│  │  ██                      │      │  │  █                       │      │
│  │  ████                    │      │  │  ███                     │      │
│  │  ██████                  │      │  │  █████                   │      │
│  │  ████████                │      │  │  ███████                 │      │
│  │  ──────────────          │      │  │  ──────────────          │      │
│  │  0  2  4  6  8  10 (¢)  │      │  │  0  2  4  6  8  10 (¢)  │      │
│  └──────────────────────────┘      │  └──────────────────────────┘      │
└────────────────────────────────────┴────────────────────────────────────┘
```

**경고 조건:**
- No 방향 Edge 실현율 > 1.5 → "z를 낮춰야 한다" 경고
- No 방향 Edge 실현율 < 0.5 → "z를 높여야 한다" 경고
- 양방향 차이가 0.3 이상 → "방향별 z 분리 필요" 제안

---

### 3E: bet365 교차 검증 효과

```
┌─────────────────────────────────────────────────────────────┐
│  bet365 Cross-Validation Effect                              │
│                                                              │
│  평균 수익 (¢/거래)                                          │
│  +4¢ ┤  ██                                                   │
│      │  ██                                                   │
│  +3¢ ┤  ██                                                   │
│      │  ██     ██                                            │
│  +2¢ ┤  ██     ██                                            │
│      │  ██     ██                                            │
│  +1¢ ┤  ██     ██                                            │
│      │  ██     ██                                            │
│   0¢ ┤──██─────██──────                                      │
│      │  HIGH   LOW                                           │
│      │  (n=98) (n=47)                                        │
│                                                              │
│  Validation Value: +2.1¢/trade  🟢                          │
│  → bet365 검증이 가치 있음. kelly_multiplier 유지.            │
│                                                              │
│  승률 비교:                                                  │
│  HIGH: 61.2%  │  LOW: 48.9%  │  차이: +12.3%p               │
└─────────────────────────────────────────────────────────────┘
```

---

### 3F: Preliminary → Confirmed 추적

```
┌─────────────────────────────────────────────────────────────┐
│  Preliminary Detection Performance                           │
│                                                              │
│  Total Preliminary Events: 156                               │
│                                                              │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  ████████████████████████████████████████░░░░░░░░   │    │
│  │  Confirmed Match: 149 (95.5%)                       │    │
│  │  ░░░░ VAR Cancelled: 4 (2.6%)                       │    │
│  │  ░░░ False Alarm: 3 (1.9%)                          │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                              │
│  Rapid Entry Readiness:                                      │
│  ✅ Accuracy > 0.95 (0.955)                                 │
│  ✅ VAR rate < 0.03 (0.026)                                 │
│  ✅ Hypothetical P&L > 0 (+$42.30)                          │
│  ✅ Trades >= 200 (287)                                      │
│  → All conditions met. Rapid Entry: ACTIVATABLE             │
│                                                              │
│  Rapid Entry Hypothetical P&L:                               │
│  +$50 ┤          ╱──────                                    │
│       │     ╱───╱                                           │
│  +$25 ┤────╱                                                │
│       │   ╱                                                 │
│    $0 ┤──╱                                                  │
│       └──┬──┬──┬──┬──┬──                                    │
│         W1  W4  W8  W12 W16                                  │
└─────────────────────────────────────────────────────────────┘
```

---

### 3G: 적응적 파라미터 히스토리

```
┌─────────────────────────────────────────────────────────────┐
│  Parameter Evolution                                         │
│                                                              │
│  K_frac                              z (보수성)              │
│  0.50 ┤              ╱──    1.8 ┤──╲                        │
│       │         ╱───╱          │   ╲                        │
│  0.40 ┤    ╱───╱              1.6 ┤    ╲────────            │
│       │───╱                        │             ╲           │
│  0.30 ┤╱                     1.4 ┤              ╲──        │
│  0.25 ┤                      1.2 ┤                          │
│       └──┬──┬──┬──┬──            └──┬──┬──┬──┬──           │
│         W1  W5  W10 W15             W1  W5  W10 W15         │
│                                                              │
│  Current Parameters:                                         │
│  ┌──────────────────────────────────────────────────┐       │
│  │  K_frac:           0.42  (started: 0.25)         │       │
│  │  z:                1.4   (started: 1.645)        │       │
│  │  LOW multiplier:   0.5   (unchanged)             │       │
│  │  Cooldown:         13s   (started: 15s)          │       │
│  │  Rapid Entry:      OFF   (conditions met)        │       │
│  │  bet365 Auto Exit: OFF   (n=18, need 30)         │       │
│  └──────────────────────────────────────────────────┘       │
│                                                              │
│  Phase: B (Adaptive Live)                                    │
│  → Next milestone: 300 trades for Phase C                    │
│    Current: 287 / 300                                        │
└─────────────────────────────────────────────────────────────┘
```

**파라미터별 변경 이력:** 각 변경에 "왜 바뀌었는가" 주석:

```
Week 12: K_frac 0.35 → 0.40
  Reason: Edge Realization = 0.88 (≥ 0.8) for 3 consecutive weeks

Week 10: z 1.645 → 1.4
  Reason: No-direction Edge Realization = 1.52 (> 1.5) — too conservative

Week 8: Cooldown 15s → 13s
  Reason: Suppressed profitable rate = 62% (> 60%)
```

---

## 알림 체계

대시보드를 항상 볼 수 없으므로 핵심 이벤트는 Push 알림:

### 알림 분류

| 이벤트 | 심각도 | 채널 | 예시 메시지 |
|--------|--------|------|-----------|
| 포지션 진입 | ℹ️ Info | Slack | `📈 ENTRY: ARS-CHE Over2.5 YES @45¢ ×15 (EV=3.2¢, HIGH)` |
| 포지션 청산 (수익) | ✅ | Slack | `💰 EXIT: ARS-CHE Over2.5 YES settled @100¢. P&L: +$8.25` |
| 포지션 청산 (손실) | ⚠️ | Slack | `📉 EXIT: LIV-MCI Over2.5 YES settled @0¢. P&L: -$5.50` |
| Drawdown > 10% | 🔴 | Slack + Telegram | `🚨 Drawdown 12.3% ($615/$5000). Review required.` |
| Live Odds WS 장애 | 🔴 | Slack + Telegram | `🔴 Live Odds WS disconnected. Fallback to 2-Layer mode.` |
| Live Score 5회 실패 | 🔴 | Slack + Telegram | `🔴 Live Score polling failed 5x. Match ARS-CHE frozen.` |
| PRELIMINARY > 30초 | ⚠️ | Slack | `⚠️ PRELIMINARY state >30s for ARS-CHE. Possible VAR review.` |
| 건강 지표 "위험" | 🔴 | Slack + Telegram | `🔴 Edge Realization dropped to 0.45. Consider pausing.` |
| 일일 P&L 요약 | ℹ️ | Slack (매일) | `📊 Daily: 4 matches, 7 trades, P&L: +$23.50, DD: 2.1%` |
| 주간 리포트 | ℹ️ | Slack (매주) | `📊 Weekly: 12 matches, 23 trades, P&L: +$142, Brier: 0.198` |

### 알림 구현

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

# 사용 예시
await send_alert(
    AlertSeverity.CRITICAL,
    "Max Drawdown Exceeded",
    f"Drawdown {dd_pct:.1f}% (${dd_amount:.0f}/${bankroll:.0f})\n"
    f"Action: All new entries frozen. Manual review required.",
    match_id="ARS-CHE-20251018"
)
```

---

## 구현 로드맵 — 시스템 Phase에 맞춘 점진적 확장

### Phase 0 (페이퍼 트레이딩) — 최소 기능 대시보드

가장 먼저 만들어야 하는 것:

| 우선순위 | 뷰 | 이유 |
|---------|-----|------|
| ⭐ 필수 | **1B: P_true vs P_kalshi vs P_bet365 차트** | "모델이 작동하는가"의 핵심 판단 |
| ⭐ 필수 | 1A: 경기 상태 헤더 | 기본 상황 인지 |
| ⭐ 필수 | 1E: 이벤트 로그 | 이벤트 처리 디버깅 |
| ⭐ 필수 | 1D: 시그널 + 가상 포지션 | 가상 거래 추적 |
| 필요 | 2B: 포지션 테이블 (가상) | 포지션 전체 파악 |
| 필요 | 2C: 가상 P&L 타임라인 | 성과 추적 |
| 선택 | 1C: μ 감쇠 차트 | 모델 내부 동작 확인 |

**기술 구현:**
- React + Recharts (프론트)
- FastAPI + WebSocket (서버)
- Redis Pub/Sub (실시간 데이터)
- SQLite (초기에는 PostgreSQL 대신 경량 DB)

### Phase A (보수적 라이브) — 리스크 모니터링 추가

```
추가:
├── 2A: 리스크 대시보드 (L1/L2/L3 시각화) ← 실제 돈이 걸리므로 필수
├── 3A: 모델 건강 대시보드 (7개 게이지)
├── 3C: 누적 P&L + Drawdown
└── 알림: Slack 연동 (Drawdown 경고, 주문 알림, 장애 알림)
```

**기술 업그레이드:**
- SQLite → PostgreSQL + TimescaleDB
- Slack Webhook 연동

### Phase B (적응적 라이브) — 분석 도구 추가

```
추가:
├── 3B: Calibration Plot
├── 3D: 방향별 분석 (Yes vs No)
├── 3E: bet365 검증 효과
├── 3F: Preliminary 정확도 + Rapid Entry 가상 P&L
├── 3G: 파라미터 히스토리
└── 알림: Telegram 추가 (Critical 이벤트)
```

### Phase C (성숙 라이브) — 자동화 + 고급 기능

```
추가:
├── 파라미터 자동 조정 이력 + 승인 UI
│   (자동 조정 제안 → 운영자 승인 → 적용)
├── Rapid Entry 활성화/비활성화 토글 (UI에서 직접)
├── 멀티 시즌 트렌드 분석
├── 경기 리플레이 모드 (과거 경기를 타임라인으로 되감기)
└── PDF 주간/월간 리포트 자동 생성
```

---

## 레이아웃 구조

### 메인 화면 구성

```
┌─────────────────────────────────────────────────────────────────┐
│  Navigation Bar                                                  │
│  [Live Matches] [Portfolio] [Analytics] [Settings]  🟣 PAPER    │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌─── Live Matches Tab ──────────────────────────────────────┐  │
│  │                                                            │  │
│  │  ┌── Match 1 ──────────┐  ┌── Match 2 ──────────┐       │  │
│  │  │ 1A: Header          │  │ 1A: Header          │       │  │
│  │  │ 1B: Price Chart     │  │ 1B: Price Chart     │       │  │
│  │  │ 1C: μ Chart         │  │ 1C: μ Chart         │       │  │
│  │  │ 1D: Signals/Pos     │  │ 1D: Signals/Pos     │       │  │
│  │  └─────────────────────┘  └─────────────────────┘       │  │
│  │                                                            │  │
│  │  ┌── Match 3 ──────────┐  ┌── Match 4 ──────────┐       │  │
│  │  │ ...                 │  │ ...                 │       │  │
│  │  └─────────────────────┘  └─────────────────────┘       │  │
│  │                                                            │  │
│  │  ┌── Event Log (공유, 전 경기) ──────────────────────────┐│  │
│  │  │ 1E: 통합 이벤트 로그 (경기별 필터 가능)                ││  │
│  │  └─────────────────────────────────────────────────────┘│  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌─── Portfolio Tab ─────────────────────────────────────────┐  │
│  │  2A: Risk Dashboard  │  2B: Position Table               │  │
│  │  2C: P&L Timeline                                        │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌─── Analytics Tab ─────────────────────────────────────────┐  │
│  │  3A: Health Dashboard │  3B: Calibration   │ 3C: P&L     │  │
│  │  3D: Directional      │  3E: bet365 Effect │ 3F: Prelim  │  │
│  │  3G: Parameter History                                    │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 경기 패널 확대/축소

- **타일 모드:** 2×2 또는 3×3 그리드로 동시 표시 (기본)
- **포커스 모드:** 특정 경기를 클릭하면 전체 화면으로 확대
  - 1B 차트가 크게 표시되고, 1C + 1D + 1E가 사이드바로 이동
- **최소화 모드:** 경기가 HALFTIME이면 자동으로 최소화 (헤더만 표시)

---

## 대시보드가 답해야 하는 질문 — 요약

| 질문 | Layer | 핵심 뷰 |
|------|-------|---------|
| "지금 이 경기에서 무슨 일이?" | Layer 1 | 1A 헤더 + 1E 로그 |
| "모델이 시장보다 나은가?" | Layer 1 | 1B 3자 가격 차트 ⭐ |
| "이벤트가 올바르게 처리됐나?" | Layer 1 | 1C μ 차트 + 1E 로그 |
| "지금 어디에 베팅 중인가?" | Layer 1 | 1D 시그널/포지션 |
| "돈이 어디에 얼마나?" | Layer 2 | 2A 리스크 + 2B 포지션 |
| "오늘 수익은?" | Layer 2 | 2C P&L 타임라인 |
| "시스템이 건강한가?" | Layer 3 | 3A 건강 대시보드 |
| "모델이 편향되어 있는가?" | Layer 3 | 3B Calibration + 3D 방향별 |
| "bet365 검증이 가치 있는가?" | Layer 3 | 3E bet365 효과 |
| "Rapid Entry 켜도 되는가?" | Layer 3 | 3F Preliminary 정확도 |
| "파라미터를 어떻게 조정?" | Layer 3 | 3G 파라미터 히스토리 |
