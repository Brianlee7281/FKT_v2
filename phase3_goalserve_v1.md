# Phase 3: Live Trading Engine — Goalserve Full Package

## 개요

경기 시작부터 종료까지 1초 단위로 작동하는 동적 프라이싱(Dynamic Pricing) 엔진.

시간이 흐름에 따라 옵션의 시간 가치가 소멸하듯(Theta Decay),
경기 잔여 시간에 따른 득점 기댓값을 연속적으로 깎아내리면서도,
퇴장이나 득점 같은 불연속적 이벤트(Jump)가 발생할 때마다
확률 공간을 즉각적으로 재조정한다.

Phase 1에서 학습한 파라미터와 Phase 2에서 설정한 초기 조건을 바탕으로,
**잔여 기대 득점 μ 계산 → 진짜 확률 P_true 산출**의 사이클을
매초 반복한다.

이 과정을 5개의 Step으로 분해한다.

### 아키텍처 패러다임 전환: 3-Layer 감지

Goalserve 풀 패키지의 Live Odds WebSocket(<1초)이
Phase 3의 아키텍처를 근본적으로 바꾼다.

**원래 설계 (2-Layer):**
```
방어선 1: Goalserve Live Score REST (3~8초) → 이벤트 확인
방어선 2: Kalshi 호가 급변 (1~2초) → ob_freeze
```

**풀 패키지 (3-Layer):**
```
방어선 1: Goalserve Live Odds WebSocket (<1초) → 최초 감지
방어선 2: Kalshi 호가 급변 (1~2초) → 교차 확인
방어선 3: Goalserve Live Score REST (3~8초) → 권위적 확인
```

이벤트 소스별 역할:

| 소스 | 프로토콜 | 지연 | 제공 데이터 | 역할 |
|------|---------|------|-----------|------|
| **Goalserve Live Odds** | **WebSocket PUSH** | **<1초** | 스코어, 분, 피리어드, bet365 배당, 볼 포지션, 경기 상태 | **1차 감지 + ob_freeze** |
| Kalshi API | WebSocket | 1~2초 | 호가창 | 교차 확인 + 거래 |
| **Goalserve Live Score** | **REST 폴링 3초** | 3~8초 | 골스코어러, 카드 상세, 교체, VAR | **권위적 확인 + 상세 정보** |

핵심 통찰:
- **Live Odds** = "무언가 발생했다"를 **가장 먼저** 알려줌 (스코어 변동, 배당 급변)
- **Live Score** = "정확히 무엇이 발생했는가"를 알려줌 (누가 골을 넣었나, VAR 취소인가)

둘 다 필요한 이유: Live Odds에서 스코어가 1-0 → 1-1로 바뀐 건 알지만,
그게 정규 골인지, 자책골인지, VAR 취소되는 건지는 Live Score에서만 확인 가능하다.

### 강도 함수 (Phase 1에서 확정)

$$\lambda_H(t \mid X, \Delta S) = \exp\!\left(a_H + b_{i(t)} + \gamma^H_{X(t)} + \delta_H(\Delta S(t))\right)$$

$$\lambda_A(t \mid X, \Delta S) = \exp\!\left(a_A + b_{i(t)} + \gamma^A_{X(t)} + \delta_A(\Delta S(t))\right)$$

| 기호 | 의미 | 변경 트리거 |
|------|------|-----------|
| $a_H, a_A$ | 경기별 기본 강도 | 경기 중 불변 |
| $b_{i(t)}$ | 시간 구간별 프로파일 | 기저함수 경계 통과 시 |
| $\gamma^H_{X(t)}$ | 레드카드 → 홈 패널티 | 레드카드 발생 시 (Jump) |
| $\gamma^A_{X(t)}$ | 레드카드 → 어웨이 패널티 | 레드카드 발생 시 (Jump) |
| $\delta_H(\Delta S)$ | 스코어차 홈 전술 효과 | 골 발생 시 (Jump) |
| $\delta_A(\Delta S)$ | 스코어차 어웨이 전술 효과 | 골 발생 시 (Jump) |

---

## Input Data

**Phase 2 산출물:**

| 항목 | 용도 |
|------|------|
| `LiveFootballQuantModel` 인스턴스 | 전체 파라미터 + 초기 상태 |
| $P_{grid}[0..100]$ + $P_{fine\_grid}$ | 행렬 지수함수 사전 계산 |
| $Q_{off\_normalized}$ (4×4) | MC용 정규화 전이 확률 |
| $C_{time}$, $T_{exp}$ | 시간 상수 |
| `DELTA_SIGNIFICANT` | 해석적/MC 모드 결정 플래그 |

**실시간 데이터 스트림 (3-소스):**

| 소스 | Goalserve 엔드포인트 | 데이터 |
|------|---------------------|--------|
| Live Odds WS | `wss://goalserve.com/liveodds/{api_key}/{match_id}` | 스코어, bet365 배당, 분, 상태, 볼 포지션 |
| Live Score REST | `GET /getfeed/{api_key}/soccerlive/home?json=1` | 골스코어러, 카드, 교체, VAR |
| Kalshi WS | Kalshi WebSocket API | 호가창 (Bid/Ask + Depth) |

---

## Step 3.1: 비동기 실시간 데이터 수신 및 상태 머신 (Event Loop & State Machine)

### 목표

물리적 시간(초)과 경기 상태(State)를 동시에 추적하며,
3-Layer 이벤트 감지 체계와 preliminary → confirmed 2단계 처리를 구현한다.

### 엔진 상태 머신 (Engine Phase)

```
FIRST_HALF ──(전반 종료)──▶ HALFTIME ──(후반 킥오프)──▶ SECOND_HALF ──(종료)──▶ FINISHED
```

| 엔진 상태 | 시간 범위 | 프라이싱 | 주문 |
|----------|----------|---------|------|
| `FIRST_HALF` | $[0,\; 45+\alpha_1]$ | 활성 | 활성 |
| `HALFTIME` | 약 15분 | **동결** | **중단** |
| `SECOND_HALF` | $[45+\alpha_1+\delta_{HT},\; T_m]$ | 활성 | 활성 |
| `FINISHED` | — | 최종 정산 | — |

하프타임 동안 λ(t) = 0이므로, 프라이싱을 계속하면
"시간은 흐르는데 골이 안 나오는" 허구의 감쇠가 발생한다.
`HALFTIME` 상태에서는 프라이싱과 주문을 모두 동결한다.

### 이벤트 상태 머신 (Event State)

3-Layer 감지를 위한 이벤트 처리 상태:

```
IDLE ──(Live Odds score 변동)──▶ PRELIMINARY_DETECTED
  │                                    │
  │                               ob_freeze = True
  │                               μ 사전 계산 (잠정)
  │                                    │
  │                    ┌───────────────┴───────────────┐
  │                    ▼                               ▼
  │              CONFIRMED                       FALSE_ALARM
  │              (Live Score 확인)                (3틱 안정 or 10초 타임아웃)
  │                    │                               │
  │              ┌─────┴─────┐                    ob_freeze = False
  │              ▼           ▼                    상태 유지
  │         VAR 취소 아님   VAR 취소                    │
  │              │           │                         │
  │         S,ΔS,X 확정   상태 롤백                     │
  │         cooldown 15초  ob_freeze = False            │
  │         ob_freeze=F         │                      │
  │              │              │                      │
  └──────────────┴──────────────┴──────────────────────┘
                 ▼
               IDLE (복귀)
```

### 수학적 상태 변수

```
t           : 현재 실질 플레이 시간 (하프타임 제외)
S(t)        : 현재 스코어 (S_H, S_A)
X(t)        : 마르코프 상태 ∈ {0, 1, 2, 3}
ΔS(t)       : 현재 득점차 = S_H - S_A
engine_phase: {FIRST_HALF, HALFTIME, SECOND_HALF, FINISHED}
event_state : {IDLE, PRELIMINARY_DETECTED, CONFIRMED}
cooldown    : bool (이벤트 후 15초 주문 차단)
ob_freeze   : bool (이상 감지 시 주문 차단)
T           : 현재 적용 중인 경기 종료 예정 시간
```

### EventSource 추상화

```python
class EventSource(ABC):
    """엔진과 데이터 소스를 분리하는 추상 계층"""
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
    # 이벤트별 추가 필드
    score: Optional[Tuple[int, int]] = None
    team: Optional[str] = None
    minute: Optional[float] = None
    period: Optional[str] = None
    var_cancelled: Optional[bool] = None
    scorer_id: Optional[str] = None
    delta: Optional[float] = None
```

### 소스 1: Goalserve Live Odds WebSocket (1차 감지, <1초)

```python
class GoalserveLiveOddsSource(EventSource):
    """
    WebSocket PUSH — <1초 지연.
    bet365 인플레이 배당 + 경기 info(스코어, 분, 상태).
    
    Goalserve Live Odds 응답 구조:
    {
      "info": {
        "score": "0:0",
        "minute": "45",
        "period": "Paused",
        "ball_pos": "x23;y46",
        "state": "1015"
      },
      "markets": {
        "1777": {  // Fulltime Result
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
            
            # ─── 스코어 변동 감지 ───
            new_score = self._parse_score(info["score"])
            if self._last_score is not None and new_score != self._last_score:
                
                # 스코어 감소? → VAR 취소 가능성
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
            
            # ─── 피리어드 변동 감지 ───
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
            
            # ─── 추가시간 진입 감지 ───
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
            
            # ─── 배당 급변 감지 ───
            markets = parsed.get("markets", {})
            odds_delta = self._compute_odds_delta(markets)
            if odds_delta >= self.ODDS_THRESHOLD:
                # 스코어 변동 동반 여부 확인
                concurrent_score = (new_score != self._last_score) if self._last_score else False
                yield NormalizedEvent(
                    type="odds_spike",
                    source="live_odds",
                    confidence="preliminary",
                    delta=odds_delta,
                    timestamp=time.time()
                )
            
            # ─── 볼 포지션 + 경기 상태 (로깅/향후 확장) ───
            self._ball_pos = info.get("ball_pos", "")
            self._game_state = info.get("state", "")
    
    def _parse_score(self, score_str: str) -> Tuple[int, int]:
        """'1:0' → (1, 0)"""
        parts = score_str.split(":")
        return (int(parts[0]), int(parts[1]))
    
    def _parse_minute(self, minute_str: str) -> Optional[float]:
        """'45' → 45.0, '90+3' → 93.0, '' → None"""
        if not minute_str:
            return None
        if "+" in minute_str:
            base, extra = minute_str.split("+")
            return float(base) + float(extra)
        return float(minute_str)
    
    def _compute_odds_delta(self, markets: dict) -> float:
        """Fulltime Result 마켓의 Home 배당 변화율 계산"""
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

### 소스 2: Goalserve Live Score REST (권위적 확인, 3~8초)

```python
class GoalserveLiveScoreSource(EventSource):
    """
    REST 폴링 3초 — 3~8초 지연.
    골스코어러, 카드 상세, 교체, VAR 정보.
    
    Goalserve Live Score 응답 구조:
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
        """이전 폴링 결과와 비교하여 변화 감지"""
        
        # ─── 스코어 변동 (골 확정) ───
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
                    var_cancelled=False,  # Live Score 이벤트에서 추출
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
        
        # ─── 레드카드 감지 ───
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
        
        # ─── 피리어드 변경 ───
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

### 비동기 루프 구조 (3-소스)

```python
async def run_engine(model: LiveFootballQuantModel):
    """3개 코루틴 동시 실행"""
    await asyncio.gather(
        tick_loop(model),                # 매 1초: μ 재계산 + P_true 산출
        live_odds_listener(model),       # WebSocket: <1초 이벤트 감지
        live_score_poller(model),        # REST 3초: 권위적 확인
    )

async def tick_loop(model):
    """코루틴 1: 매 1초 틱"""
    while model.engine_phase != FINISHED:
        if model.engine_phase in (FIRST_HALF, SECOND_HALF):
            model.t += 1/60

            # Step 3.2: 잔여 기대 득점
            μ_H, μ_A = compute_remaining_mu(model)

            # Step 3.4: 프라이싱
            P_true, σ_MC = await step_3_4_async(model, μ_H, μ_A)

            # 주문 허용 판단 — 3조건 모두 충족 시에만
            order_allowed = (
                not model.cooldown
                and not model.ob_freeze
                and model.event_state == IDLE
            )

            # Phase 4로 전달
            emit_to_phase4(P_true, σ_MC, order_allowed, model)

        await asyncio.sleep(1)

async def live_odds_listener(model):
    """코루틴 2: Live Odds WebSocket (<1초)"""
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
    """코루틴 3: Live Score REST (3초 폴링)"""
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

### 이벤트 핸들러 — 2단계 처리 (Preliminary → Confirmed)

#### Preliminary 핸들러 (Live Odds WebSocket, <1초)

```python
def handle_preliminary_goal(model, event: NormalizedEvent):
    """
    Live Odds에서 스코어 변동 감지.
    아직 VAR 확인 전이므로 '잠정' 처리.
    """
    # 1. 즉시 ob_freeze + 상태 전이
    model.ob_freeze = True
    model.event_state = PRELIMINARY_DETECTED
    
    # 2. 득점 팀 추론 (스코어 차이로)
    preliminary_score = event.score
    if preliminary_score[0] > model.S[0]:
        scoring_team = "home"
    elif preliminary_score[1] > model.S[1]:
        scoring_team = "away"
    else:
        log.warning("Score changed but neither team increased — ignoring")
        return
    
    # 3. 잠정 ΔS 계산
    preliminary_delta_S = preliminary_score[0] - preliminary_score[1]
    
    # 4. μ 사전 계산 (executor에서 비동기로)
    #    Live Score 확정 시 0ms로 P_true 산출하기 위한 워밍업
    asyncio.create_task(precompute_preliminary_mu(
        model, preliminary_delta_S, scoring_team
    ))
    
    # 5. 잠정 데이터 캐시
    model.preliminary_cache = {
        "score": preliminary_score,
        "delta_S": preliminary_delta_S,
        "scoring_team": scoring_team,
        "timestamp": event.timestamp,
    }
    
    log.info(f"PRELIMINARY goal: {model.S} → {preliminary_score} "
             f"(team={scoring_team})")

def handle_score_rollback(model, event: NormalizedEvent):
    """
    Live Odds에서 스코어가 감소 — VAR 취소 가능성.
    preliminary 상태면 즉시 롤백.
    """
    if model.event_state == PRELIMINARY_DETECTED:
        log.warning(f"Score rollback: {model.preliminary_cache['score']} → "
                    f"{event.score} — likely VAR cancellation")
        model.event_state = IDLE
        model.ob_freeze = False
        model.preliminary_cache = {}
    else:
        log.warning(f"Score rollback in state {model.event_state} — logging only")

def handle_odds_spike(model, event: NormalizedEvent):
    """
    배당 급변만 감지 (스코어 변동 없이).
    골인지 레드카드인지 모름 → ob_freeze만 설정하고 Live Score 대기.
    """
    model.ob_freeze = True
    log.warning(f"Odds spike: Δ={event.delta:.3f} — awaiting Live Score confirmation")

def handle_period_change(model, event: NormalizedEvent):
    """Live Odds에서 피리어드 변경 감지"""
    if event.period in ("Paused", "Half", "HT"):
        model.engine_phase = HALFTIME
        log.info("HALFTIME detected via Live Odds")
    elif event.period in ("2nd Half", "2nd"):
        model.engine_phase = SECOND_HALF
        log.info("SECOND HALF started via Live Odds")
```

#### Confirmed 핸들러 (Live Score REST, 3~8초)

```python
def handle_confirmed_goal(model, event: NormalizedEvent):
    """
    Live Score에서 골 확정.
    VAR 취소 여부, 득점자, 어시스트까지 확인 완료.
    """
    # 1. VAR 취소 체크
    if event.var_cancelled:
        model.event_state = IDLE
        model.ob_freeze = False
        model.preliminary_cache = {}
        log.info("Goal VAR cancelled — state rolled back")
        return
    
    # 2. 스코어 확정
    if event.team == "localteam":
        model.S = (model.S[0] + 1, model.S[1])
    else:
        model.S = (model.S[0], model.S[1] + 1)
    model.delta_S = model.S[0] - model.S[1]
    
    # 3. μ 재계산 — preliminary 사전 계산 재사용
    if (model.preliminary_cache
        and model.preliminary_cache.get("delta_S") == model.delta_S):
        # 사전 계산 결과 재사용 → 0ms
        model.μ_H = model.preliminary_cache["μ_H"]
        model.μ_A = model.preliminary_cache["μ_A"]
        log.info("Using pre-computed μ from preliminary stage")
    else:
        # 사전 계산 없거나 delta_S 불일치 → 새로 계산
        model.μ_H, model.μ_A = recompute_mu(model)
    
    # 4. 상태 전이
    model.cooldown = True
    model.ob_freeze = False
    model.event_state = IDLE
    model.preliminary_cache = {}
    asyncio.create_task(cooldown_timer(model, duration=15))
    
    log.info(f"CONFIRMED goal: S={model.S}, ΔS={model.delta_S}, "
             f"team={event.team}, scorer={event.scorer_id}")

def handle_confirmed_red_card(model, event: NormalizedEvent):
    """
    레드카드는 Live Score에서만 확정 가능.
    Live Odds에서는 배당 급변(odds_spike)으로만 간접 감지.
    """
    # 1. 마르코프 상태 전이
    if event.team == "localteam":
        if model.X == 0: model.X = 1      # 11v11 → 10v11
        elif model.X == 2: model.X = 3    # 11v10 → 10v10
    else:  # visitorteam
        if model.X == 0: model.X = 2      # 11v11 → 11v10
        elif model.X == 1: model.X = 3    # 10v11 → 10v10
    
    # 2. μ 재계산 — γ^H, γ^A 변경 반영
    model.μ_H, model.μ_A = recompute_mu(model)
    
    # 3. 상태 전이
    model.cooldown = True
    model.ob_freeze = False
    model.event_state = IDLE
    asyncio.create_task(cooldown_timer(model, duration=15))
    
    log.info(f"CONFIRMED red card: X={model.X}, team={event.team}")

def handle_confirmed_period(model, event: NormalizedEvent):
    """Live Score에서 피리어드 확정 — Live Odds와 교차 확인"""
    if event.period == "Halftime" and model.engine_phase != HALFTIME:
        log.warning("Halftime confirmed by Live Score but not detected by Live Odds")
        model.engine_phase = HALFTIME
    elif event.period == "Finished":
        model.engine_phase = FINISHED
```

#### 보조 함수

```python
async def cooldown_timer(model, duration: int = 15):
    """쿨다운 타이머: duration초 후 cooldown 해제"""
    await asyncio.sleep(duration)
    model.cooldown = False
    log.info(f"Cooldown expired after {duration}s")

def handle_live_score_failure(model):
    """Live Score 폴링 5회 연속 실패 → 신규 주문 중단"""
    model.ob_freeze = True  # 안전 모드
    log.error("Live Score source failure — freezing all orders")
```

### ob_freeze 해제 조건

```python
def check_ob_freeze_release(model):
    """
    매 틱에서 호출. ob_freeze 해제 조건 확인.
    
    해제 조건 (하나라도 충족 시):
    1. Goalserve 이벤트 감지 → 상태 업데이트 완료 + cooldown 진입
    2. 3틱(3초) 연속 안정화 (Live Odds 배당 변동 < 임계)
    3. 10초 타임아웃 (오인 방지)
    """
    if not model.ob_freeze:
        return
    
    # 조건 1: 이벤트로 설명됨 (cooldown이 이어받음)
    if model.cooldown:
        model.ob_freeze = False
        return
    
    # 조건 2: 3틱 안정화
    if model._ob_stable_ticks >= 3:
        model.ob_freeze = False
        model._ob_stable_ticks = 0
        log.info("ob_freeze released: 3-tick stabilization")
        return
    
    # 조건 3: 10초 타임아웃
    elapsed = time.time() - model._ob_freeze_start
    if elapsed >= 10:
        model.ob_freeze = False
        log.info("ob_freeze released: 10s timeout")
```

### 타임라인 비교

**원래 설계 (REST만):**
```
t=0.0s  골 발생 (경기장)
t=1.5s  Kalshi MM 호가 반응 시작
t=2.0s  Kalshi 호가 ΔP ≥ 5¢ → ob_freeze (방어선 2)
t=2~6s  blind spot (ob_freeze로 방어, "왜" 동결했는지 모름)
t=6.0s  Live Score 폴링 감지 (방어선 1)
t=6.0s  상태 업데이트 + cooldown 15초
t=21s   정상 운영 재개
```
→ blind spot: **2~6초**

**풀 패키지 (3-Layer):**
```
t=0.0s  골 발생 (경기장)
t=0.5s  bet365 배당 급변 → Live Odds WS 수신
t=0.5s  score "0:0"→"1:0" 감지 → PRELIMINARY + ob_freeze (방어선 1)
t=0.5s  μ 사전 계산 시작 (executor)
t=1.5s  Kalshi MM 호가 반응 → 교차 확인 (방어선 2)
t=5.0s  Live Score 폴링: 골 확인 + 득점자 + VAR 상태 (방어선 3)
t=5.0s  CONFIRMED → μ 확정 (사전 계산 재사용, 0ms) + cooldown 15초
t=20s   정상 운영 재개
```
→ blind spot: **~0.5초**

### 결과물

매 틱마다 업데이트되는 상태 벡터:

$$\text{State}(t) = (t,\; S,\; X,\; \Delta S,\; \text{engine\_phase},\; \text{event\_state},\; \text{cooldown},\; \text{ob\_freeze},\; T)$$

---

## Step 3.2: 잔여 기대 득점 계산 (Remaining Expected Goals)

### 목표

현재 시간 t부터 경기 종료 T까지 남은 시간 동안의
홈팀 기대 득점 μ_H(t, T)와 어웨이팀 기대 득점 μ_A(t, T)를 계산한다.

### 적분의 구조

남은 시간 [t, T]를 기저함수 경계에서 잘라 L개의 소구간으로 나눈다:

$$[t, T] = [t, \tau_1) \cup [\tau_1, \tau_2) \cup \cdots \cup [\tau_{L-1}, T]$$

### 마르코프 변조 적분 공식

팀별 γ를 적용:

$$\boxed{\mu_H(t, T) = \sum_{\ell=1}^{L} \sum_{j=0}^{3} \overline{P}_{X(t),j}^{(\ell)} \cdot \exp\!\left(a_H + b_{i_\ell} + \gamma^H_j + \delta_H(\Delta S)\right) \cdot \Delta\tau_\ell}$$

$$\boxed{\mu_A(t, T) = \sum_{\ell=1}^{L} \sum_{j=0}^{3} \overline{P}_{X(t),j}^{(\ell)} \cdot \exp\!\left(a_A + b_{i_\ell} + \gamma^A_j + \delta_A(\Delta S)\right) \cdot \Delta\tau_\ell}$$

| 항 | 의미 |
|----|------|
| $\overline{P}_{X(t),j}^{(\ell)}$ | 소구간 ℓ 동안 상태 j에 있을 평균 확률 |
| $a_T + b_{i_\ell} + \gamma^T_j + \delta_T(\Delta S)$ | 팀 T ∈ {H, A}의 순간 득점 강도 |
| $\Delta\tau_\ell$ | 소구간의 길이 (분) |

> **δ(ΔS) 고정:** ΔS는 **현재** 스코어차로 고정한다.
> 미래 골에 의한 ΔS 변화는 Step 3.4의 Monte Carlo에서 처리한다.

### 행렬 지수함수 조회

```python
def get_transition_prob(model, dt_min: float) -> np.ndarray:
    """
    P_grid 또는 P_fine_grid에서 전이 확률 조회.
    경기 종료 직전에는 fine grid 사용.
    """
    if dt_min <= 5 and hasattr(model, 'P_fine_grid'):
        # Fine grid: 10초 단위 (경기 종료 직전)
        dt_10sec = int(round(dt_min * 6))
        dt_10sec = max(0, min(30, dt_10sec))
        return model.P_fine_grid[dt_10sec]
    else:
        # Standard grid: 1분 단위
        dt_round = max(0, min(100, round(dt_min)))
        return model.P_grid[dt_round]
```

### Preliminary 사전 계산

```python
async def precompute_preliminary_mu(model, preliminary_delta_S, scoring_team):
    """
    Live Odds에서 골 감지 시 즉시 호출.
    Live Score 확정 전에 μ를 미리 계산 → 확정 시 0ms.
    """
    loop = asyncio.get_event_loop()
    
    # δ 인덱스
    di = max(0, min(4, preliminary_delta_S + 2))
    
    # μ_H, μ_A 계산 (해석적 or MC, executor에서)
    if model.X == 0 and preliminary_delta_S == 0 and not model.DELTA_SIGNIFICANT:
        # 해석적 — 즉시
        μ_H, μ_A = analytical_remaining_mu(model, preliminary_delta_S)
    else:
        # MC — executor
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
    
    # 캐시에 저장
    model.preliminary_cache["μ_H"] = μ_H
    model.preliminary_cache["μ_A"] = μ_A
    
    log.info(f"Preliminary μ computed: μ_H={μ_H:.3f}, μ_A={μ_A:.3f}")
```

### 결과물

매 틱마다 μ_H(t, T), μ_A(t, T).

---

## Step 3.3: 불연속적 충격 처리 (Discrete Event Handler)

### 이벤트 소스별 역할 매트릭스

| 이벤트 | 1차 감지 (Live Odds WS, <1초) | 확정 (Live Score REST, 3~8초) |
|--------|------------------------------|------------------------------|
| **골** | score 필드 변동 → PRELIMINARY | 골스코어러 + VAR 상태 → CONFIRMED |
| **레드카드** | 배당 급변 → ob_freeze (유형 불명) | redcards diff → CONFIRMED |
| **하프타임** | period "Paused" → engine_phase 전환 | period "Half" → 교차 확인 |
| **추가시간** | minute > 45 / > 90 → T 롤링 | minute 필드 → 교차 확인 |
| **VAR 리뷰** | 배당 진동 (올갔다 내려갔다) | var_cancelled 필드 |
| **VAR 취소** | score 감소 → score_rollback | var_cancelled=True |
| **교체** | 감지 불가 | substitutions diff (로깅) |

### 이벤트 1: 골 — 2단계 처리

**Stage 1 — Preliminary (Live Odds, <1초):**
- score 변동 감지 → `PRELIMINARY_DETECTED`
- ob_freeze = True
- 잠정 ΔS로 μ 사전 계산
- Phase 4 주문 차단

**Stage 2 — Confirmed (Live Score, 3~8초):**
- var_cancelled 확인
- **if not cancelled:** S 확정 → δ_H(ΔS), δ_A(ΔS) 적용 → μ 확정 → cooldown 15초
- **if cancelled:** 상태 롤백 → ob_freeze 해제

### 이벤트 2: 레드카드 — Live Score에서만 확정

Live Odds에서는 배당 급변으로 "뭔가 발생"만 감지 가능.
스코어 변동이 없는 배당 급변 → 레드카드 or VAR 리뷰 가능성.

| 전이 | 트리거 | γ^H 변경 | γ^A 변경 |
|------|--------|----------|----------|
| 0 → 1 | 홈 퇴장 | 0 → γ^H₁ < 0 (홈 하락) | 0 → γ^A₁ > 0 (어웨이 상승) |
| 0 → 2 | 어웨이 퇴장 | 0 → γ^H₂ > 0 (홈 상승) | 0 → γ^A₂ < 0 (어웨이 하락) |
| 1 → 3 | 어웨이 추가 퇴장 | γ^H₁ → γ^H₁+γ^H₂ | γ^A₁ → γ^A₁+γ^A₂ |
| 2 → 3 | 홈 추가 퇴장 | γ^H₂ → γ^H₁+γ^H₂ | γ^A₂ → γ^A₁+γ^A₂ |

레드카드 확정 시 μ_H, μ_A가 **반대 방향**으로 변동:
홈 퇴장 → μ_H 하락 + μ_A 상승.

### 이벤트 3: 하프타임

| 동작 | 1차 (Live Odds) | 확정 (Live Score) |
|------|-----------------|-------------------|
| 전반 종료 | period="Paused" → HALFTIME | period="HT" → 교차 확인 |
| 후반 시작 | period="2nd Half" → SECOND_HALF | status 변경 → 교차 확인 |

### 이벤트 4: 추가시간

Live Odds와 Live Score 모두 minute를 제공하므로 교차 검증 가능.
Step 3.5에서 상세 처리.

### 쿨다운 (Cooldown)

| 항목 | 값 |
|------|---|
| 이벤트 수신 → 확정 지연 | <1초 (Live Odds) + 3~8초 (Live Score) |
| 쿨다운 길이 | **15초** (confirmed 시점부터) |
| 쿨다운 중 P_true 계산 | 계속 (모니터링) |
| 쿨다운 중 주문 | **차단** |

### 결과물

재조정된 상태 벡터 (S, X, ΔS, μ_H, μ_A, T) 및 event_state/cooldown/ob_freeze 상태.

---

## Step 3.4: 프라이싱 — True Probability 산출

### 목표

잔여 기대 득점 μ_H, μ_A를 바탕으로,
Kalshi 호가창과 비교할 수 있는 진짜 확률(P_true)을 산출한다.

### 독립성 가정 분석

δ(ΔS) 도입으로 한 팀이 골을 넣으면 양 팀의 λ가 동시에 변하므로
홈/어웨이 득점의 독립성이 깨진다.

X = 0, ΔS = 0에서 출발하더라도, 첫 번째 골 발생 시 ΔS = ±1이 되어
δ(±1) ≠ 0이면 이후 양 팀 강도가 연동된다.
따라서 해석적 Poisson/Skellam은 **1차 근사**에 불과하다.

### 하이브리드 프라이싱

| 조건 | 방법 | 정확도 |
|------|------|--------|
| X=0, ΔS=0, δ 비유의 | 해석적 푸아송/스켈람 | **정확** |
| X=0, ΔS=0, δ 유의 | 해석적 (1차 근사) | **근사** — δ 피드백 무시 |
| X≠0 or ΔS≠0 | Monte Carlo 시뮬레이션 | **정확** (충분한 N에서) |

> **실용적 지침:** δ 값이 작으면 (|δ| < 0.1) 해석적 근사의 오차가 MC 표준오차보다 작을 수 있다.
> δ 값이 큰 경우 (|δ| ≥ 0.15), ΔS = 0에서도 MC를 사용하는 것이 안전하다.
> Numba JIT + Executor 구조에서 MC 오버헤드는 ~0.5ms/경기이므로 항상 MC도 실용적 대안이다.

### 로직 A: 해석적 프라이싱 (X=0, ΔS=0)

$G = S_H + S_A$ (현재까지의 총 득점 수)라 하면:

**Over/Under:**

$$P_{true}(\text{Over } N\text{.5}) = \begin{cases} 1 & \text{if } G > N \\ 1 - \sum_{k=0}^{N-G} \frac{\mu_{total}^k \cdot e^{-\mu_{total}}}{k!} & \text{if } G \leq N \end{cases}$$

**Match Odds (스켈람 분포):**

$$P_{true}(\text{Home Win}) = \sum_{D=1}^{\infty} e^{-(\mu_H + \mu_A)} \left(\frac{\mu_H}{\mu_A}\right)^{D/2} I_{|D|}(2\sqrt{\mu_H \mu_A})$$

해석적 모드는 메인 스레드에서 즉시 실행 (~0.1ms).

### 로직 B: Monte Carlo 프라이싱 (X≠0 or ΔS≠0)

#### Numba JIT 컴파일된 MC 코어

```python
@njit(cache=True)
def mc_simulate_remaining(
    t_now, T_end, S_H, S_A, state, score_diff,
    a_H, a_A,
    b,                  # shape (6,)
    gamma_H, gamma_A,   # shape (4,) 각각
    delta_H, delta_A,   # shape (5,) 각각
    Q_diag,             # shape (4,)
    Q_off,              # shape (4,4) — 정규화된 전이 확률
    basis_bounds,       # shape (7,)
    N, seed
):
    """
    Returns: final_scores — shape (N, 2)
    팀별 γ + 정규화 Q_off 사용.
    """
    np.random.seed(seed)
    results = np.empty((N, 2), dtype=np.int32)

    for sim in range(N):
        s = t_now
        sh, sa = S_H, S_A
        st = state
        sd = score_diff

        while s < T_end:
            # 현재 기저함수 인덱스
            bi = 0
            for k in range(6):
                if s >= basis_bounds[k] and s < basis_bounds[k + 1]:
                    bi = k
                    break

            # δ 인덱스: ΔS → {0:≤-2, 1:-1, 2:0, 3:+1, 4:≥+2}
            di = max(0, min(4, sd + 2))

            # 팀별 γ 사용
            lam_H = np.exp(a_H + b[bi] + gamma_H[st] + delta_H[di])
            lam_A = np.exp(a_A + b[bi] + gamma_A[st] + delta_A[di])
            lam_red = -Q_diag[st]
            lam_total = lam_H + lam_A + lam_red

            if lam_total <= 0:
                break

            # 다음 이벤트까지 대기 시간
            dt = -np.log(np.random.random()) / lam_total
            s_next = s + dt

            # 기저함수 경계 또는 경기 종료 확인
            next_bound = T_end
            for k in range(7):
                if basis_bounds[k] > s:
                    next_bound = min(next_bound, basis_bounds[k])
                    break

            if s_next >= next_bound:
                s = next_bound
                continue

            s = s_next

            # 이벤트 결정
            u = np.random.random() * lam_total
            if u < lam_H:
                sh += 1
                sd += 1
            elif u < lam_H + lam_A:
                sa += 1
                sd -= 1
            else:
                # 정규화된 Q_off 사용
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

**성능:**

| 구현 | 소요 시간 | 10경기 동시 |
|------|----------|-----------|
| 순수 Python | ~50ms | ~500ms ❌ |
| **Numba @njit** | **~0.5ms** | **~5ms ✅** |

#### Executor 디커플링

```python
mc_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mc")

async def step_3_4_async(model, μ_H, μ_A):
    """메인 이벤트 루프를 블로킹하지 않는 비동기 프라이싱"""

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

        # Stale 체크
        if my_version != model._mc_version:
            return None, None
        if model.event_state == PRELIMINARY_DETECTED:
            return None, None

        P_true = aggregate_markets(final_scores, model.S)
        σ_MC = compute_mc_stderr(P_true, N_MC)
        return P_true, σ_MC
```

> **MC 시드 결정론:** `hash(match_id, t, S_H, S_A, X)` 기반 시드를 사용하여
> 동일 상태에서 동일 결과가 나오도록 재현성을 확보한다.
> 디버깅과 백테스트에서 결과를 정확히 재현할 수 있다.

### 시장별 확률 산출 (MC 결과 집계)

하나의 MC 배치로 **모든 시장의 확률이 동시에** 나온다:

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

        # Correct Score (상위 확률 스코어만)
        # ...
    }
```

### 결과물

매 1초마다:
- P_true(t): 각 활성 시장별 진짜 확률
- σ_MC(t): Monte Carlo 표준오차 (해석적: 0)
- pricing_mode: Analytical / Monte Carlo

---

## Step 3.5: 추가시간 실시간 처리 (Stoppage Time)

### 목표

Phase 2에서 설정한 $T_{exp}$를 경기 진행에 따라 실시간으로 보정한다.

### 이중 소스 교차 검증

Live Odds(minute, <1초)와 Live Score(timer, 3~8초) 모두 minute를 제공하므로
교차 검증으로 데이터 신뢰성을 높인다.

```python
class StoppageTimeManager:
    def __init__(self, T_exp: float, rolling_horizon: float = 1.5):
        self.T_exp = T_exp
        self.rolling_horizon = rolling_horizon
        self.first_half_stoppage = False
        self.second_half_stoppage = False
        self._lo_minute = None   # Live Odds의 분
        self._ls_minute = None   # Live Score의 분

    def update_from_live_odds(self, minute: float, period: str) -> float:
        """Live Odds WebSocket — 더 빠른 갱신 (<1초)"""
        self._lo_minute = minute
        return self._compute_T(minute, period)

    def update_from_live_score(self, minute: float, period: str) -> float:
        """Live Score REST — 권위적 갱신 (3~8초)"""
        self._ls_minute = minute

        # 교차 검증: 두 소스의 분이 2분 이상 차이나면 경고
        if self._lo_minute and abs(self._lo_minute - minute) > 2:
            log.warning(
                f"Minute mismatch: LiveOdds={self._lo_minute}, "
                f"LiveScore={minute}"
            )
        return self._compute_T(minute, period)

    def _compute_T(self, minute: float, period: str) -> float:
        # Phase B: 전반 추가시간
        if period in ("1st Half", "1st") and minute > 45:
            if not self.first_half_stoppage:
                self.first_half_stoppage = True
            # T_game은 변경하지 않고, 기저함수 경계만 조정
            # (전반 종료 시각은 하프타임 진입으로 확정)
            return self.T_exp

        # Phase C: 후반 추가시간
        if period in ("2nd Half", "2nd") and minute > 90:
            if not self.second_half_stoppage:
                self.second_half_stoppage = True
            # T_game을 롤링으로 업데이트
            return minute + self.rolling_horizon

        # Phase A: 정규 시간
        return self.T_exp
```

> **Phase B vs Phase C 구분:**
> Phase B(전반 추가시간)에서는 T_game을 변경하지 않는다 — 전반 종료는
> 하프타임 진입 이벤트로 확정되므로, T_game은 후반 추가시간(Phase C)에서만 롤링한다.

### 추가시간 불확실성 모델링 (선택적 확장)

Monte Carlo에서 각 경로의 T를 추가시간 분포(Log-Normal 또는 Gamma)에서
샘플링하면 자연스럽게 불확실성이 반영된다.

### 결과물

실시간 업데이트된 T.

---

## Phase 3 → Phase 4 핸드오프

| 항목 | 값 | 업데이트 빈도 |
|------|---|-------------|
| P_true(t) | 각 시장별 진짜 확률 | 매 1초 |
| σ_MC(t) | MC 표준오차 | 매 1초 (해석적: 0) |
| **order_allowed** | **NOT cooldown AND NOT ob_freeze AND event_state == IDLE** | 매 1초 + 이벤트 시 |
| pricing_mode | Analytical / Monte Carlo | 이벤트 시 전환 |
| μ_H, μ_A | 잔여 기대 득점 | 매 1초 (로깅용) |
| engine_phase | 현재 경기 단계 | 피리어드 변경 시 |
| **event_state** | **IDLE / PRELIMINARY / CONFIRMED** | 이벤트 시 |
| **P_bet365(t)** | **bet365 인플레이 내재 확률** | **매 Push (<1초)** |
| **ball_pos, game_state** | **볼 포지션 + 경기 상태** | **매 Push (로깅/향후 확장)** |

---

## Phase 3 파이프라인 요약

```
[킥오프 — engine_phase: FIRST_HALF]
              │
              ▼
┌─────────────────────────────────────────────────────────────────┐
│  Step 3.1: 상태 머신 + 3-Layer 감지                               │
│                                                                 │
│  ┌────────────────────┐  ┌──────────────────┐  ┌─────────────┐ │
│  │ Live Odds WS       │  │ Kalshi WS        │  │ Live Score  │ │
│  │ (<1초, PUSH)       │  │ (1~2초)          │  │ (3~8초,REST)│ │
│  │                    │  │                  │  │             │ │
│  │ score 변동:        │  │ 호가 수신:       │  │ 골 확정:    │ │
│  │ → PRELIMINARY      │  │ → 교차 확인      │  │ → CONFIRMED │ │
│  │ → ob_freeze        │  │ → Phase 4 전달   │  │ → VAR 체크  │ │
│  │                    │  │                  │  │             │ │
│  │ 배당 급변:         │  │                  │  │ 레드카드:   │ │
│  │ → ob_freeze        │  │                  │  │ → CONFIRMED │ │
│  │                    │  │                  │  │             │ │
│  │ period/minute:     │  │                  │  │ period:     │ │
│  │ → 하프타임/추가시간 │  │                  │  │ → 교차확인  │ │
│  │                    │  │                  │  │             │ │
│  │ bet365 배당:       │  │                  │  │             │ │
│  │ → P_bet365 (Ph4용) │  │                  │  │             │ │
│  └────────┬───────────┘  └──────────────────┘  └──────┬──────┘ │
│           │                                           │        │
│           └──────────────┬────────────────────────────┘        │
│                          ▼                                     │
│           Event State Machine                                  │
│           IDLE → PRELIMINARY → CONFIRMED → COOLDOWN → IDLE    │
│                          ↘ FALSE_ALARM → IDLE                  │
│                          ↘ VAR_CANCELLED → IDLE                │
│                                                                 │
│           order_allowed = NOT cooldown                          │
│                          AND NOT ob_freeze                      │
│                          AND event_state == IDLE                │
└──────────────────┬──────────────────────────────────────────────┘
                   │
        ┌──────────┴──────────┐
        │ (매 1초 틱)         │ (이벤트 감지 시)
        ▼                     ▼
┌──────────────────┐  ┌────────────────────────────────────────┐
│  Step 3.2:       │  │  Step 3.3: 불연속 충격 처리              │
│  잔여 기대 득점   │  │                                        │
│                  │  │  • 골: preliminary → confirmed (VAR)   │
│  • Piecewise 적분│  │  • 레드카드: X 전이, γ^H/γ^A 변경     │
│  • P_grid 조회   │  │  • 하프타임: 동결/재개                  │
│  • 팀별 γ 적용   │  │  • 추가시간: T 롤링 (이중 소스 교차)    │
│  • δ(ΔS) 보정   │  │  • μ 사전 계산 (preliminary)            │
│  Output: μ_H,μ_A │  └────────┬───────────────────────────────┘
└────────┬─────────┘           │
         │                     │
         └──────────┬──────────┘
                    ▼
┌─────────────────────────────────────────────────────────────────┐
│  Step 3.4: 프라이싱 (True Probability)                            │
│                                                                 │
│  ┌───────────────────────┐  ┌──────────────────────────────┐   │
│  │ X=0, ΔS=0, δ 비유의?  │  │ 그 외                        │   │
│  │ → 해석적 (즉시,0.1ms) │  │ → Numba MC (ThreadPool)     │   │
│  │   σ_MC = 0            │  │   N=50000, ~0.5ms/경기      │   │
│  └───────────┬───────────┘  │   결정론적 시드 (재현성)      │   │
│              │               │   Stale + PRELIMINARY 체크   │   │
│              │               └──────────────┬───────────────┘   │
│              └──────────┬───────────────────┘                   │
│                         ▼                                       │
│  Output: P_true(t), σ_MC(t), pricing_mode                      │
└──────────────────┬──────────────────────────────────────────────┘
                   │
                   ▼
┌─────────────────────────────────────────────────────────────────┐
│  Step 3.5: 추가시간 처리 (이중 소스 교차 검증)                     │
│  • Live Odds minute (<1초) + Live Score timer (3~8초)           │
│  • Phase B (전반): T_game 유지, 하프타임으로 확정                 │
│  • Phase C (후반): T = minute + 1.5분 롤링                      │
└──────────────────┬──────────────────────────────────────────────┘
                   │
                   ▼
         [Phase 4: Arbitrage & Execution]
         (order_allowed + P_true + σ_MC + P_bet365)
```
