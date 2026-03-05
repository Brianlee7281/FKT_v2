# Implementation Blueprint — 24/7 Automated System

## 개요

킨 순간부터 24/7 자동으로 돌아가는 Kalshi 축구 퀀트 자동트레이딩 시스템의 구현 청사진.

사람의 개입 없이:
- 매일 경기 스케줄을 스캔하고
- 킥오프 60분 전에 자동으로 Pre-Match를 실행하고
- 경기 중 실시간으로 트레이딩하고
- 경기 종료 후 자동 정산 + 사후 분석하고
- 주기적으로 모델을 재학습하고
- 장애 시 자동 복구한다

### 설계 원칙

1. **무인 운영 (Lights-Out):** 정상 상태에서 사람의 개입 불필요
2. **Graceful Degradation:** 부분 장애 시 안전하게 축소 운영
3. **Self-Healing:** 일시적 장애는 자동 복구
4. **Observable:** 모든 상태가 대시보드 + 알림으로 가시화
5. **Auditable:** 모든 결정과 거래가 영구 기록

---

## 시스템 아키텍처

### 프로세스 구조 — 5개 상시 프로세스 + 2개 주기 프로세스

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Single Server (VPS)                          │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  Process 1: SCHEDULER (항상 실행)                             │   │
│  │  • 매일 경기 스케줄 스캔 → 경기별 Job 생성                     │   │
│  │  • 킥오프 60분 전: MatchEngine 인스턴스 스폰                  │   │
│  │  • 킥오프 후: MatchEngine 상태 모니터링                       │   │
│  │  • 경기 종료 후: 정산 + 사후 분석 트리거                      │   │
│  └─────────────────────────┬───────────────────────────────────┘   │
│                            │ spawn/monitor                         │
│  ┌─────────────────────────▼───────────────────────────────────┐   │
│  │  Process 2~N: MATCH_ENGINE (경기당 1개, 동적 스폰)            │   │
│  │  • Phase 2 (Pre-Match) → Phase 3 (Live) → Phase 4 (Exec)   │   │
│  │  • 3개 asyncio 코루틴: tick_loop + live_odds + live_score    │   │
│  │  • 경기 종료 시 자동 소멸                                     │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  Process N+1: DASHBOARD_SERVER (항상 실행)                    │   │
│  │  • FastAPI + WebSocket → React 프론트엔드 서빙               │   │
│  │  • Redis에서 실시간 데이터 구독 → 브라우저로 Push             │   │
│  │  • PostgreSQL에서 분석 데이터 쿼리                            │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  Process N+2: ALERT_SERVICE (항상 실행)                       │   │
│  │  • Redis에서 이벤트 구독 → Slack/Telegram 발송               │   │
│  │  • 시스템 건강 모니터링 (프로세스 생존, 메모리, CPU)            │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  Process N+3: DATA_COLLECTOR (항상 실행)                      │   │
│  │  • Goalserve Fixtures/Results + Live Game Stats 수집         │   │
│  │  • 과거 경기 데이터 DB 적재 (Phase 1 학습용)                  │   │
│  │  • Pregame Odds 수집 + 아카이브                              │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─────────────── 주기 프로세스 ────────────────────────────────┐   │
│  │  CRON 1: RECALIBRATION (트리거 시 또는 시즌 시작)             │   │
│  │  • Phase 1 전체 재실행 (Step 1.1~1.5)                       │   │
│  │  • 새 프로덕션 파라미터 생성 → hot-reload                    │   │
│  │                                                              │   │
│  │  CRON 2: ANALYTICS_DAILY (매일 자정)                         │   │
│  │  • Step 4.6 사후 분석 집계                                   │   │
│  │  • 적응적 파라미터 조정                                       │   │
│  │  • 일일 리포트 생성 + Slack 발송                              │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  ┌─────────── 인프라 ──────────────────────────────────────────┐   │
│  │  Redis          │  PostgreSQL + TimescaleDB                  │   │
│  │  (실시간 메시지) │  (영구 저장)                                │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
```

### 24시간 타임라인 — 자동 운영 흐름

```
00:00 ─── ANALYTICS_DAILY 실행
│         • 전날 정산 완료 경기 사후 분석
│         • 적응적 파라미터 조정
│         • 일일 리포트 Slack 발송
│
06:00 ─── SCHEDULER: 오늘 경기 스캔
│         • Goalserve Fixtures에서 오늘 날짜 경기 목록 수집
│         • 각 경기의 킥오프 시간 파악
│         • DB에 match_jobs 테이블 생성
│
(예: 킥오프 12:30 경기)
11:30 ─── SCHEDULER: MatchEngine 스폰 (킥오프 60분 전)
│         • Phase 2 Step 2.1~2.5 자동 실행
│         • Sanity Check → GO/HOLD/SKIP 자동 판정
│         • GO이면 대기, SKIP이면 엔진 소멸
│
12:25 ─── Pre-Kickoff 최종 확인 (킥오프 5분 전)
│         • 라인업 재확인
│         • 연결 상태 확인
│
12:30 ─── Phase 3 시작: Live Trading Engine
│         • 3-Layer 감지 활성화
│         • 매 1초 틱 루프
│         • 골/레드카드 이벤트 처리
│         • Phase 4 시그널 → 주문 전송
│
14:15 ─── 경기 종료 (예상)
│         • Goalserve status "Finished" 감지
│         • 잔여 포지션 만기 정산 대기
│         • Kalshi 정산 결과 수신
│
14:20 ─── MatchEngine 정리
│         • 거래 로그 PostgreSQL 최종 기록
│         • Redis에서 해당 경기 데이터 TTL 설정
│         • MatchEngine 프로세스 소멸
│
(다음 경기들도 동일한 사이클)
│
24:00 ─── ANALYTICS_DAILY 다시 실행 (하루 마감)
```

---

## Process 1: SCHEDULER — 자동 스케줄링 엔진

### 역할

매일 경기 스케줄을 스캔하고, 킥오프 시간에 맞춰 MatchEngine을 자동으로 스폰/관리한다.

### 구현

```python
# src/scheduler/main.py

import asyncio
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler

class MatchScheduler:
    """
    24/7 실행. 매일 경기 스캔 → 킥오프 전 MatchEngine 스폰.
    """
    def __init__(self, config: SystemConfig):
        self.config = config
        self.scheduler = AsyncIOScheduler()
        self.active_engines: Dict[str, MatchEngine] = {}
        self.db = PostgresClient(config.db_url)
        self.goalserve = GoalserveClient(config.goalserve_api_key)
        self.alerter = AlertService(config.alert_config)

    async def start(self):
        """시스템 시작점"""
        # 1. 매일 06:00 UTC — 오늘 경기 스캔
        self.scheduler.add_job(
            self.scan_today_matches,
            'cron', hour=6, minute=0
        )

        # 2. 매 5분 — 스케줄 갱신 (경기 시간 변경, 연기 감지)
        self.scheduler.add_job(
            self.refresh_schedule,
            'interval', minutes=5
        )

        # 3. 매 10초 — 엔진 건강 모니터링
        self.scheduler.add_job(
            self.monitor_engines,
            'interval', seconds=10
        )

        self.scheduler.start()

        # 시작 시 즉시 오늘 경기 스캔
        await self.scan_today_matches()

        # 무한 대기
        while True:
            await asyncio.sleep(1)

    async def scan_today_matches(self):
        """Goalserve Fixtures에서 오늘 경기 목록 수집"""
        today = datetime.utcnow().strftime("%d.%m.%Y")

        for league_id in self.config.target_leagues:
            fixtures = await self.goalserve.get_fixtures(league_id, today)

            for match in fixtures:
                kickoff = parse_kickoff_time(match)
                match_id = match["id"]

                # DB에 저장
                await self.db.upsert_match_job(
                    match_id=match_id,
                    league_id=league_id,
                    kickoff_time=kickoff,
                    home_team=match["localteam"]["name"],
                    away_team=match["visitorteam"]["name"],
                    status="SCHEDULED"
                )

                # 킥오프 60분 전에 엔진 스폰 예약
                spawn_time = kickoff - timedelta(minutes=60)
                if spawn_time > datetime.utcnow():
                    self.scheduler.add_job(
                        self.spawn_engine,
                        'date',
                        run_date=spawn_time,
                        args=[match_id],
                        id=f"spawn_{match_id}",
                        replace_existing=True
                    )

        log.info(f"Scanned {today}: {len(fixtures)} matches scheduled")

    async def spawn_engine(self, match_id: str):
        """MatchEngine 인스턴스 생성 + Phase 2 실행"""
        if match_id in self.active_engines:
            return  # 이미 실행 중

        try:
            engine = MatchEngine(match_id, self.config)
            self.active_engines[match_id] = engine

            # Phase 2 실행 (Pre-Match Initialization)
            result = await engine.run_prematch()

            if result.verdict == "SKIP":
                log.info(f"Match {match_id} SKIPPED by sanity check")
                await self.db.update_match_status(match_id, "SKIPPED")
                del self.active_engines[match_id]
                return

            if result.verdict == "HOLD":
                await self.alerter.send(
                    "WARNING",
                    f"Match {match_id} on HOLD — manual review needed"
                )

            # 킥오프 대기 → Phase 3+4 자동 시작
            asyncio.create_task(engine.run_live())

            await self.db.update_match_status(match_id, "LIVE")
            log.info(f"Engine spawned for {match_id}")

        except Exception as e:
            log.error(f"Failed to spawn engine for {match_id}: {e}")
            await self.alerter.send("CRITICAL", f"Engine spawn failed: {match_id}\n{e}")

    async def monitor_engines(self):
        """활성 엔진 건강 모니터링"""
        for match_id, engine in list(self.active_engines.items()):
            # 경기 종료 확인
            if engine.is_finished():
                await self.handle_match_finished(match_id, engine)
                continue

            # 건강 체크
            if not engine.is_healthy():
                await self.alerter.send(
                    "CRITICAL",
                    f"Engine unhealthy: {match_id}\n{engine.health_report()}"
                )

    async def handle_match_finished(self, match_id: str, engine: MatchEngine):
        """경기 종료 후 정리"""
        # 1. 최종 거래 로그 기록
        await engine.finalize_logs()

        # 2. 사후 분석 예약 (즉시 또는 배치)
        await self.db.update_match_status(match_id, "FINISHED")

        # 3. 엔진 소멸
        await engine.shutdown()
        del self.active_engines[match_id]

        log.info(f"Engine for {match_id} shut down after match end")
```

### 대상 리그 설정

```python
# config/leagues.yaml
target_leagues:
  - id: "1204"     # EPL
    name: "Premier League"
    priority: 1
  - id: "1399"     # La Liga
    name: "La Liga"
    priority: 1
  - id: "1229"     # Bundesliga
    name: "Bundesliga"
    priority: 2
  - id: "1269"     # Serie A
    name: "Serie A"
    priority: 2
  - id: "1221"     # Ligue 1
    name: "Ligue 1"
    priority: 2

# Kalshi에서 거래 가능한 리그만 포함.
# 리그 추가 시 Phase 1 재학습 필요.
```

---

## Process 2~N: MATCH_ENGINE — 경기별 트레이딩 엔진

### 역할

단일 경기의 전체 라이프사이클 (Phase 2 → 3 → 4 → 정산)을 관리한다.

### 생명주기

```
SPAWNED ──(Phase 2)──▶ PREMATCH_READY ──(킥오프)──▶ LIVE
    │                       │                          │
    │                    SKIPPED                   FINISHED
    │                  (sanity fail)                    │
    └──────────────────────────────────────────────── SHUTDOWN
```

### 구현

```python
# src/engine/match_engine.py

class MatchEngine:
    """
    경기당 1개 인스턴스. Phase 2~4의 전체 파이프라인을 관리.
    """
    def __init__(self, match_id: str, config: SystemConfig):
        self.match_id = match_id
        self.config = config
        self.model = None  # LiveFootballQuantModel
        self.state = "SPAWNED"

        # 인프라 연결
        self.redis = RedisClient(config.redis_url)
        self.db = PostgresClient(config.db_url)

        # Goalserve 소스
        self.live_odds_source = GoalserveLiveOddsSource(config.goalserve_api_key)
        self.live_score_source = GoalserveLiveScoreSource(config.goalserve_api_key, match_id)

        # Kalshi
        self.kalshi = KalshiClient(config.kalshi_api_key)
        self.execution = ExecutionLayer(config.trading_mode)  # PAPER or LIVE

    async def run_prematch(self) -> SanityResult:
        """Phase 2: Pre-Match Initialization"""
        self.state = "PREMATCH"

        # Step 2.1~2.3
        prematch_data = await collect_prematch_data(self.match_id, self.config)
        X_match = apply_feature_mask(prematch_data, self.config.feature_mask)
        a_H, a_A, C_time = compute_a_parameters(X_match, self.config)

        # Step 2.4
        sanity = combined_sanity_check(a_H, a_A, prematch_data.odds_features)

        if sanity.verdict in ("GO", "GO_WITH_CAUTION"):
            # Step 2.5
            self.model = initialize_model(
                match_id=self.match_id,
                a_H=a_H, a_A=a_A, C_time=C_time,
                config=self.config
            )
            self.state = "PREMATCH_READY"

        return sanity

    async def run_live(self):
        """Phase 3+4: Live Trading — 킥오프까지 대기 후 자동 시작"""
        # 킥오프 대기
        await self._wait_for_kickoff()

        # 킥오프 5분 전 최종 확인
        if not await self._pre_kickoff_check():
            self.state = "SKIPPED"
            return

        self.state = "LIVE"

        try:
            # 3개 코루틴 동시 실행
            await asyncio.gather(
                self._tick_loop(),
                self._live_odds_listener(),
                self._live_score_poller(),
            )
        except Exception as e:
            log.error(f"Engine {self.match_id} crashed: {e}")
            await self._emergency_shutdown(e)
        finally:
            self.state = "FINISHED"

    async def _tick_loop(self):
        """코루틴 1: 매 1초 틱"""
        while self.model.engine_phase != "FINISHED":
            if self.model.engine_phase in ("FIRST_HALF", "SECOND_HALF"):
                self.model.t += 1/60

                # Step 3.2
                μ_H, μ_A = compute_remaining_mu(self.model)

                # Step 3.4
                P_true, σ_MC = await step_3_4_async(self.model, μ_H, μ_A)

                if P_true is not None:
                    # Step 4.2~4.5
                    await self._execute_trading_cycle(P_true, σ_MC)

                # 상태 스냅샷 → Redis (대시보드용)
                await self._publish_state_snapshot(P_true, σ_MC, μ_H, μ_A)

            await asyncio.sleep(1)

    async def _execute_trading_cycle(self, P_true, σ_MC):
        """Phase 4: 시그널 → 사이징 → 주문 (Phase 4 v2)"""
        order_allowed = (
            not self.model.cooldown
            and not self.model.ob_freeze
            and self.model.event_state == "IDLE"
        )

        for market in self.config.active_markets:
            P_bet365 = self.model.ob_sync.bet365_implied.get(market)

            # Step 4.2: 시그널 생성 (2-pass VWAP + 시장 정합성 확인)
            # generate_signal 내부에서:
            #   Pass 1: best ask/bid로 rough qty 산출
            #   Pass 2: rough qty의 VWAP로 최종 EV 산출
            #   + bet365 시장 정합성 확인 → alignment_status
            signal = generate_signal(
                P_true[market], σ_MC,
                self.model.ob_sync,     # VWAP 계산용 호가 깊이 포함
                P_bet365,
                self.config.fee_rate, self.config.z,
                self.config.K_frac,     # 2-pass에서 rough qty 산출에 필요
                self.model.bankroll,
                market
            )

            if signal.direction != "HOLD" and order_allowed:
                # Step 4.3: 사이징 (signal.P_kalshi는 이미 VWAP 실효 가격)
                f = compute_kelly(signal, self.config.fee_rate, self.config.K_frac)
                amount = apply_risk_limits(f, self.match_id, self.model.bankroll)

                if amount > 0:
                    # Step 4.5: 주문 (PAPER 모드: VWAP + 슬리피지 시뮬레이션)
                    fill = await self.execution.execute_order(
                        signal, amount, self.model.ob_sync
                    )
                    if fill:
                        await self._record_trade(signal, fill)

            # Step 4.4: 기존 포지션 청산 평가 (방향별 수식 적용)
            P_kalshi_bid = self.model.ob_sync.kalshi_best_bid
            for pos in self.model.positions.get(market, []):
                exit_signal = await evaluate_exit(
                    pos, P_true[market], σ_MC,
                    P_kalshi_bid, P_bet365,
                    self.config.fee_rate, self.config.z,
                    self.model.t, self.model.T
                )
                if exit_signal:
                    await self._execute_exit(pos, exit_signal)

    async def _publish_state_snapshot(self, P_true, σ_MC, μ_H, μ_A):
        """Redis에 상태 스냅샷 발행 (대시보드 + 알림 서비스 소비)"""
        snapshot = {
            "match_id": self.match_id,
            "timestamp": time.time(),
            "t": self.model.t,
            "score": self.model.S,
            "X": self.model.X,
            "delta_S": self.model.delta_S,
            "mu_H": μ_H,
            "mu_A": μ_A,
            "P_true": P_true,
            "sigma_MC": σ_MC,
            "engine_phase": self.model.engine_phase,
            "event_state": self.model.event_state,
            "cooldown": self.model.cooldown,
            "ob_freeze": self.model.ob_freeze,
            "P_bet365": self.model.ob_sync.bet365_implied,
            "P_kalshi_bid": self.model.ob_sync.kalshi_best_bid,
            "P_kalshi_ask": self.model.ob_sync.kalshi_best_ask,
            "positions": serialize_positions(self.model.positions),
            "bankroll": self.model.bankroll,
        }
        await self.redis.publish(f"match:{self.match_id}:state", json.dumps(snapshot))

    async def _emergency_shutdown(self, error: Exception):
        """비정상 종료 시 안전 처리"""
        # 1. 모든 미체결 주문 취소
        await self.kalshi.cancel_all_orders()

        # 2. 알림 발송
        await self.redis.publish("alerts", json.dumps({
            "severity": "CRITICAL",
            "title": f"Engine Crash: {self.match_id}",
            "body": str(error),
        }))

        # 3. 상태 기록
        await self.db.record_engine_crash(self.match_id, str(error))

    def is_finished(self) -> bool:
        return self.state == "FINISHED"

    def is_healthy(self) -> bool:
        if self.state != "LIVE":
            return True
        # 마지막 틱이 5초 이상 전이면 비정상
        return (time.time() - self.model.last_tick_time) < 5
```

---

## Process N+1: DASHBOARD_SERVER

### 구현

```python
# src/dashboard/server.py

from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles

app = FastAPI()

# React 빌드 파일 서빙
app.mount("/static", StaticFiles(directory="src/dashboard/frontend/build/static"))

@app.websocket("/ws/live/{match_id}")
async def ws_match_live(websocket: WebSocket, match_id: str):
    """경기별 실시간 데이터 스트림"""
    await websocket.accept()
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"match:{match_id}:state")

    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                await websocket.send_text(message["data"])
    finally:
        await pubsub.unsubscribe()

@app.websocket("/ws/portfolio")
async def ws_portfolio(websocket: WebSocket):
    """포트폴리오 전체 실시간 데이터"""
    await websocket.accept()
    pubsub = redis.pubsub()
    await pubsub.psubscribe("match:*:state")

    try:
        async for message in pubsub.listen():
            if message["type"] == "pmessage":
                await websocket.send_text(message["data"])
    finally:
        await pubsub.unsubscribe()

@app.get("/api/analytics/health")
async def get_health_dashboard():
    """Layer 3: 모델 건강 대시보드 데이터"""
    return await compute_health_metrics(db)

@app.get("/api/analytics/calibration")
async def get_calibration():
    """Layer 3: Calibration Plot 데이터"""
    return await compute_calibration_data(db)

@app.get("/api/analytics/pnl")
async def get_cumulative_pnl():
    """Layer 3: 누적 P&L 데이터"""
    return await compute_cumulative_pnl(db)
```

---

## Process N+2: ALERT_SERVICE

```python
# src/alerts/main.py

class AlertService:
    """Redis에서 이벤트 구독 → Slack/Telegram 발송"""

    async def start(self):
        pubsub = redis.pubsub()
        await pubsub.subscribe("alerts")
        await pubsub.psubscribe("match:*:state")

        async for message in pubsub.listen():
            if message["channel"] == "alerts":
                await self._handle_alert(json.loads(message["data"]))
            elif message["type"] == "pmessage":
                await self._check_state_alerts(json.loads(message["data"]))

    async def _check_state_alerts(self, state: dict):
        """상태 스냅샷에서 자동 알림 조건 체크"""
        # Drawdown 체크
        if state.get("drawdown_pct", 0) > 10:
            await self.send("CRITICAL", f"Drawdown {state['drawdown_pct']:.1f}%")

        # PRELIMINARY 30초 초과
        if (state.get("event_state") == "PRELIMINARY"
            and time.time() - state.get("preliminary_start", 0) > 30):
            await self.send("WARNING",
                f"PRELIMINARY >30s for {state['match_id']}. Possible VAR.")

        # 데이터 소스 장애
        if state.get("live_odds_healthy") == False:
            await self.send("CRITICAL",
                f"Live Odds WS down for {state['match_id']}")
```

---

## Process N+3: DATA_COLLECTOR

```python
# src/data/collector.py

class DataCollector:
    """
    24/7 실행. Goalserve 과거 데이터를 지속적으로 수집하여 DB에 적재.
    Phase 1 재학습의 입력 데이터를 준비한다.
    """
    async def start(self):
        scheduler = AsyncIOScheduler()

        # 매일 05:00 — 어제 경기 결과 + 스탯 수집
        scheduler.add_job(self.collect_yesterday_results, 'cron', hour=5)

        # 매 6시간 — Pregame Odds 스냅샷
        scheduler.add_job(self.collect_odds_snapshot, 'interval', hours=6)

        # 주 1회 — 과거 데이터 무결성 검증
        scheduler.add_job(self.verify_data_integrity, 'cron', day_of_week='sun', hour=3)

        scheduler.start()

    async def collect_yesterday_results(self):
        """어제 완료된 경기의 결과 + 상세 스탯을 DB에 적재"""
        yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%d.%m.%Y")

        for league_id in config.target_leagues:
            # Fixtures/Results
            fixtures = await goalserve.get_fixtures(league_id, yesterday)
            for match in fixtures:
                if match["status"] == "Full-time":
                    await db.upsert_match_result(match)

            # Live Game Stats (과거 경기)
            for match in fixtures:
                stats = await goalserve.get_match_stats(match["id"])
                if stats:
                    await db.upsert_match_stats(match["id"], stats)

            # Pregame Odds (close 배당)
            odds = await goalserve.get_odds(league_id, yesterday)
            for match_odds in odds:
                await db.upsert_match_odds(match_odds)
```

---

## CRON 1: RECALIBRATION — Phase 1 재학습

```python
# src/calibration/recalibrate.py

class Recalibrator:
    """
    Phase 1 전체 파이프라인 재실행.
    트리거: 수동, 시즌 시작, 또는 Step 4.6 자동 트리거.
    """
    async def run(self, trigger_reason: str):
        log.info(f"Recalibration started. Reason: {trigger_reason}")

        # Step 1.1: 구간 분할
        intervals = await build_intervals_from_db(self.db, self.config)

        # Step 1.2: Q 행렬
        Q = estimate_Q_matrix(intervals, self.config)

        # Step 1.3: XGBoost ML
        model, feature_mask = train_xgboost_prior(intervals, self.db, self.config)

        # Step 1.4: NLL 최적화
        params = joint_nll_optimization(intervals, model, Q, self.config)

        # Step 1.5: Validation
        validation = walk_forward_validation(intervals, params, self.config)

        if validation.passes_all_criteria():
            # 새 파라미터를 프로덕션에 배포 (hot-reload)
            new_version = await self.deploy_parameters(params, feature_mask, Q)
            log.info(f"New parameters deployed: version {new_version}")
            await self.alerter.send("INFO",
                f"Recalibration complete. New params v{new_version}")
        else:
            log.warning("Recalibration FAILED validation. Keeping old params.")
            await self.alerter.send("WARNING",
                f"Recalibration failed validation:\n{validation.report()}")

    async def deploy_parameters(self, params, feature_mask, Q):
        """
        새 파라미터를 파일로 저장 + Redis에 hot-reload 시그널 발송.
        활성 MatchEngine들이 다음 경기부터 새 파라미터를 로드.
        """
        version = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        param_dir = f"data/parameters/{version}"
        os.makedirs(param_dir)

        # 파라미터 저장
        save_params(params, f"{param_dir}/params.json")
        save_feature_mask(feature_mask, f"{param_dir}/feature_mask.json")
        save_Q_matrix(Q, f"{param_dir}/Q.npy")
        model.save_model(f"{param_dir}/xgboost.xgb")

        # 심볼릭 링크 업데이트
        symlink_path = "data/parameters/production"
        if os.path.islink(symlink_path):
            os.unlink(symlink_path)
        os.symlink(param_dir, symlink_path)

        # Hot-reload 시그널
        await redis.publish("system:param_reload", version)

        return version
```

---

## CRON 2: ANALYTICS_DAILY

```python
# src/analytics/daily.py

class DailyAnalytics:
    """매일 자정 실행. Step 4.6의 사후 분석 + 적응적 파라미터 조정."""

    async def run(self):
        # 1. 오늘 정산된 경기들의 사후 분석
        today_trades = await self.db.get_today_settled_trades()

        if not today_trades:
            return

        # 2. 11개 지표 계산
        analytics = compute_all_analytics(today_trades, self.db)

        # 3. 적응적 파라미터 조정
        param_updates = adaptive_parameter_update(analytics)
        if param_updates:
            await self.apply_param_updates(param_updates)

        # 4. Phase 1 재학습 트리거 체크
        if analytics.get("brier_score_trend") == "worsening_3weeks":
            await self.trigger_recalibration("brier_score_degradation")

        # 5. 일일 리포트 생성 + Slack 발송
        report = generate_daily_report(analytics, today_trades)
        await alerter.send("INFO", report)

        # 6. DB에 분석 결과 저장
        await self.db.save_daily_analytics(analytics)
```

---

## 파라미터 Hot-Reload

활성 MatchEngine을 재시작하지 않고 새 파라미터를 적용하는 메커니즘:

```python
# src/engine/match_engine.py 내부

async def _param_reload_listener(self):
    """Redis에서 파라미터 재로드 시그널 수신"""
    pubsub = redis.pubsub()
    await pubsub.subscribe("system:param_reload")

    async for message in pubsub.listen():
        if message["type"] == "message":
            new_version = message["data"]
            log.info(f"Parameter reload signal: v{new_version}")

            # 현재 진행 중인 경기에는 적용하지 않음
            # 다음 경기의 Phase 2에서 새 파라미터 로드
            self.config.pending_param_version = new_version
```

> **안전 규칙:** 경기 중에는 파라미터를 절대 변경하지 않는다.
> 새 파라미터는 다음 경기의 Phase 2에서 로드된다.

---

## 장애 복구 (Self-Healing)

### 프로세스 감독 — systemd

```ini
# /etc/systemd/system/kalshi-scheduler.service
[Unit]
Description=Kalshi Trading Scheduler
After=network.target redis.service postgresql.service

[Service]
Type=simple
User=kalshi
WorkingDirectory=/opt/kalshi
ExecStart=/opt/kalshi/.venv/bin/python -m src.scheduler.main
Restart=always
RestartSec=5
Environment=PYTHONPATH=/opt/kalshi

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/kalshi-dashboard.service
[Unit]
Description=Kalshi Dashboard Server
After=network.target redis.service

[Service]
Type=simple
User=kalshi
WorkingDirectory=/opt/kalshi
ExecStart=/opt/kalshi/.venv/bin/uvicorn src.dashboard.server:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/kalshi-alerts.service
# /etc/systemd/system/kalshi-collector.service
# (동일 패턴)
```

### 장애 시나리오별 자동 복구

| 장애 | 감지 | 자동 복구 | 알림 |
|------|------|----------|------|
| MatchEngine 크래시 | Scheduler monitor (10초) | 미체결 주문 취소, 포지션 유지 | 🔴 CRITICAL |
| Live Odds WS 끊김 | heartbeat 5초 | 재연결 시도 3회 → fallback to 2-Layer | 🔴 CRITICAL |
| Live Score 폴링 실패 | HTTP 3회 연속 | 재시도 + 5회 시 경기 스킵 | 🔴 CRITICAL |
| Kalshi WS 끊김 | heartbeat 10초 | 미체결 취소 → 재연결 | 🔴 CRITICAL |
| Redis 다운 | 연결 실패 | 재연결 시도 (10초 간격) + 로컬 큐 | ⚠️ WARNING |
| PostgreSQL 다운 | 연결 실패 | 로그를 로컬 파일에 버퍼링 | ⚠️ WARNING |
| Scheduler 크래시 | systemd | 자동 재시작 (5초 후) | 🔴 CRITICAL |
| 서버 리부트 | systemd enable | 전 서비스 자동 시작 | 🔴 CRITICAL |
| Goalserve API 키 만료 | HTTP 401 | 알림 → 수동 갱신 필요 | 🔴 CRITICAL |

---

## 설정 관리

### 전역 설정

```yaml
# config/system.yaml

# 트레이딩 모드
trading_mode: "paper"  # "paper" or "live"

# Goalserve
goalserve:
  api_key: "${GOALSERVE_API_KEY}"  # 환경변수에서 로드
  live_score_poll_interval: 3      # 초
  live_odds_ws_url: "wss://goalserve.com/liveodds"

# Kalshi
kalshi:
  api_key: "${KALSHI_API_KEY}"
  api_secret: "${KALSHI_API_SECRET}"
  ws_url: "wss://trading-api.kalshi.com/trade-api/ws/v2"
  rest_url: "https://trading-api.kalshi.com/trade-api/v2"

# 리스크 파라미터
risk:
  f_order_cap: 0.03
  f_match_cap: 0.05
  f_total_cap: 0.20
  initial_bankroll: 5000  # PAPER 모드용

# 트레이딩 파라미터 (적응적 조정 대상)
trading:
  K_frac: 0.25
  z: 1.645
  theta_entry: 0.02
  theta_exit: 0.005
  cooldown_seconds: 15
  low_confidence_multiplier: 0.5
  rapid_entry_enabled: false
  bet365_divergence_auto_exit: false

# 인프라
redis:
  url: "redis://localhost:6379/0"
postgres:
  url: "postgresql://kalshi:${DB_PASSWORD}@localhost:5432/kalshi"

# 알림
alerts:
  slack_webhook: "${SLACK_WEBHOOK_URL}"
  telegram_bot_token: "${TELEGRAM_BOT_TOKEN}"
  telegram_chat_id: "${TELEGRAM_CHAT_ID}"

# 대상 리그
target_leagues:
  - "1204"  # EPL
  - "1399"  # La Liga

# 대상 마켓
active_markets:
  - "over_25"
  - "home_win"
  - "away_win"
  - "btts"
```

### 환경 분리

```
config/
├── system.yaml          # 기본 설정
├── system.paper.yaml    # PAPER 모드 오버라이드
├── system.live.yaml     # LIVE 모드 오버라이드
└── secrets.env          # API 키 (gitignore)
```

---

## 데이터베이스 스키마

### PostgreSQL 테이블

```sql
-- 경기 스케줄 + 상태
CREATE TABLE match_jobs (
    match_id        TEXT PRIMARY KEY,
    league_id       TEXT NOT NULL,
    home_team       TEXT,
    away_team       TEXT,
    kickoff_time    TIMESTAMPTZ,
    status          TEXT DEFAULT 'SCHEDULED',  -- SCHEDULED/LIVE/FINISHED/SKIPPED
    sanity_verdict  TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 거래 로그 (Step 4.5, Phase 4 v2)
CREATE TABLE trade_logs (
    id              BIGSERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL,
    match_id        TEXT NOT NULL,
    market_ticker   TEXT NOT NULL,
    direction       TEXT NOT NULL,
    order_type      TEXT NOT NULL,
    quantity_ordered INT,
    quantity_filled  INT,
    limit_price     NUMERIC(6,4),
    fill_price      NUMERIC(6,4),
    P_true          NUMERIC(6,4),
    P_true_cons     NUMERIC(6,4),
    P_kalshi        NUMERIC(6,4),        -- VWAP 실효 가격 (v2: best가 아닌 VWAP)
    P_kalshi_best   NUMERIC(6,4),        -- best ask/bid (VWAP와 비교용, v2 추가)
    P_bet365        NUMERIC(6,4),
    EV_adj          NUMERIC(6,4),        -- VWAP 반영 최종 EV (v2)
    sigma_MC        NUMERIC(6,4),
    pricing_mode    TEXT,
    f_kelly         NUMERIC(6,4),
    K_frac          NUMERIC(4,2),
    alignment_status TEXT,               -- v2: "ALIGNED"|"DIVERGENT"|"UNAVAILABLE" (v1: bet365_confidence)
    kelly_multiplier NUMERIC(4,2),       -- v2: 0.8/0.5/0.6 (v1: 1.0/0.5)
    cooldown_active BOOLEAN,
    ob_freeze_active BOOLEAN,
    event_state     TEXT,
    engine_phase    TEXT,
    bankroll_before NUMERIC(10,2),
    bankroll_after  NUMERIC(10,2),
    is_paper        BOOLEAN DEFAULT FALSE,
    paper_slippage  NUMERIC(6,4)         -- v2: Paper 모드 시뮬레이션 슬리피지
);

-- 포지션 (활성 + 정산 완료)
-- v2: realized_pnl은 방향별 정산 공식으로 계산
--   Buy Yes: (settlement - entry_price) × quantity - fee
--   Buy No:  (entry_price - settlement) × quantity - fee
CREATE TABLE positions (
    id              BIGSERIAL PRIMARY KEY,
    match_id        TEXT NOT NULL,
    market_ticker   TEXT NOT NULL,
    direction       TEXT NOT NULL,       -- BUY_YES | BUY_NO
    entry_price     NUMERIC(6,4),        -- Yes 확률 공간 (Buy No: Yes를 sell한 가격)
    entry_time      TIMESTAMPTZ,
    quantity        INT,
    settlement      NUMERIC(6,4),        -- NULL if open, 1.00 or 0.00 at expiry
    realized_pnl    NUMERIC(10,2),       -- 방향별 정산 (v2)
    closed_at       TIMESTAMPTZ,
    is_paper        BOOLEAN DEFAULT FALSE
);

-- 일일 분석 결과 (Step 4.6)
CREATE TABLE daily_analytics (
    date            DATE PRIMARY KEY,
    brier_score     NUMERIC(6,4),
    delta_bs_pinnacle NUMERIC(6,4),
    edge_realization NUMERIC(6,4),
    max_drawdown_pct NUMERIC(6,4),
    bet365_alignment_value NUMERIC(6,4),  -- v2: "시장 정합성 가치" (v1: bet365_validation_value)
    preliminary_accuracy NUMERIC(6,4),
    yes_edge_realization NUMERIC(6,4),
    no_edge_realization NUMERIC(6,4),
    total_trades    INT,
    total_pnl       NUMERIC(10,2),
    K_frac          NUMERIC(4,2),
    z               NUMERIC(4,2),
    param_version   TEXT
);

-- 이벤트 로그 (TimescaleDB hypertable)
CREATE TABLE event_logs (
    time            TIMESTAMPTZ NOT NULL,
    match_id        TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    source          TEXT NOT NULL,
    confidence      TEXT,
    data            JSONB
);
SELECT create_hypertable('event_logs', 'time');

-- 틱별 스냅샷 (TimescaleDB hypertable, 분석용)
CREATE TABLE tick_snapshots (
    time            TIMESTAMPTZ NOT NULL,
    match_id        TEXT NOT NULL,
    t               NUMERIC(6,2),
    score_h         INT,
    score_a         INT,
    state_x         INT,
    delta_s         INT,
    mu_h            NUMERIC(6,4),
    mu_a            NUMERIC(6,4),
    P_true          JSONB,      -- {"over_25": 0.58, "home_win": 0.42, ...}
    P_kalshi        JSONB,
    P_bet365        JSONB,
    sigma_MC        NUMERIC(6,4),
    engine_phase    TEXT,
    event_state     TEXT
);
SELECT create_hypertable('tick_snapshots', 'time');

-- Phase 1 파라미터 버전 관리
CREATE TABLE param_versions (
    version         TEXT PRIMARY KEY,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    trigger_reason  TEXT,
    validation_report JSONB,
    is_production   BOOLEAN DEFAULT FALSE
);

-- 과거 경기 데이터 (Phase 1 학습용)
CREATE TABLE historical_matches (
    match_id        TEXT PRIMARY KEY,
    league_id       TEXT,
    date            DATE,
    home_team       TEXT,
    away_team       TEXT,
    ft_score_h      INT,
    ft_score_a      INT,
    ht_score_h      INT,
    ht_score_a      INT,
    added_time_1    INT,
    added_time_2    INT,
    summary         JSONB,    -- goals, redcards, yellowcards
    stats           JSONB,    -- team stats (shots, possession, etc.)
    player_stats    JSONB,    -- per-player stats
    odds            JSONB,    -- pregame odds (20+ bookmakers)
    lineups         JSONB,    -- formations + starting 11
    collected_at    TIMESTAMPTZ DEFAULT NOW()
);
```

---

## Final Folder Structure

```
kalshi-soccer-quant/
│
├── README.md
├── LICENSE
├── pyproject.toml                     # Python 패키지 설정 (poetry/pip)
├── Makefile                           # 빌드/배포 자동화
├── docker-compose.yml                 # Redis + PostgreSQL + 앱 (선택적)
│
├── config/
│   ├── system.yaml                    # 기본 설정
│   ├── system.paper.yaml              # PAPER 모드 오버라이드
│   ├── system.live.yaml               # LIVE 모드 오버라이드
│   ├── leagues.yaml                   # 대상 리그 목록
│   └── secrets.env                    # API 키 (gitignore)
│
├── data/
│   ├── parameters/                    # Phase 1 산출물
│   │   ├── production -> ./20250915_120000/   # 심볼릭 링크 (현재 프로덕션)
│   │   ├── 20250915_120000/           # 버전별 디렉토리
│   │   │   ├── params.json            # b[], γ^H, γ^A, δ_H, δ_A
│   │   │   ├── Q.npy                  # Q 행렬 (4×4)
│   │   │   ├── xgboost.xgb           # XGBoost 가중치
│   │   │   ├── feature_mask.json      # 선택된 피처 목록
│   │   │   ├── median_values.json     # 결측 대체용 중앙값
│   │   │   └── validation_report.json # Step 1.5 검증 결과
│   │   └── 20250801_090000/           # 이전 버전 (롤백용)
│   │       └── ...
│   │
│   └── cache/                         # 런타임 캐시
│       ├── player_rolling/            # 선수별 롤링 스탯 캐시
│       └── team_rolling/              # 팀별 롤링 스탯 캐시
│
├── src/
│   ├── __init__.py
│   │
│   ├── common/                        # 공유 유틸리티
│   │   ├── __init__.py
│   │   ├── config.py                  # SystemConfig 로더
│   │   ├── logging.py                 # 구조화된 로깅
│   │   ├── redis_client.py            # Redis Pub/Sub 래퍼
│   │   ├── db_client.py               # PostgreSQL 래퍼
│   │   └── types.py                   # 공유 데이터 타입 (NormalizedEvent, Signal, etc.)
│   │
│   ├── goalserve/                     # Goalserve API 클라이언트
│   │   ├── __init__.py
│   │   ├── client.py                  # REST API 클라이언트 (Fixtures, Stats, Odds)
│   │   ├── live_score_source.py       # GoalserveLiveScoreSource (REST 폴링)
│   │   ├── live_odds_source.py        # GoalserveLiveOddsSource (WebSocket)
│   │   └── parsers.py                 # Goalserve JSON → 내부 타입 변환
│   │
│   ├── kalshi/                        # Kalshi API 클라이언트
│   │   ├── __init__.py
│   │   ├── client.py                  # REST + WebSocket 클라이언트
│   │   ├── orderbook.py               # OrderBookSync (VWAP buy/sell, depth)
│   │   └── execution.py               # ExecutionLayer (PAPER: VWAP+슬리피지+부분체결 / LIVE: Kalshi REST)
│   │
│   ├── calibration/                   # Phase 1: Offline Calibration
│   │   ├── __init__.py
│   │   ├── step_1_1_intervals.py      # 구간 분할 (VAR 필터링, 자책골 처리)
│   │   ├── step_1_2_Q_matrix.py       # 마르코프 Q 행렬 추정
│   │   ├── step_1_3_ml_prior.py       # XGBoost Poisson 학습 + 피처 선택
│   │   ├── step_1_4_nll.py            # Joint NLL 최적화 (PyTorch)
│   │   ├── step_1_5_validation.py     # Walk-Forward CV + 진단
│   │   ├── recalibrate.py             # 재학습 오케스트레이터
│   │   └── features/                  # 피처 엔지니어링
│   │       ├── __init__.py
│   │       ├── tier1_team.py          # 팀 레벨 롤링 스탯
│   │       ├── tier2_player.py        # 선수 레벨 집계
│   │       ├── tier3_odds.py          # 배당률 피처
│   │       └── tier4_context.py       # 컨텍스트 (H/A, 휴식일, H2H)
│   │
│   ├── prematch/                      # Phase 2: Pre-Match Initialization
│   │   ├── __init__.py
│   │   ├── step_2_1_data_collection.py    # 라인업 + 스탯 + 배당 수집
│   │   ├── step_2_2_feature_selection.py  # feature_mask 적용
│   │   ├── step_2_3_a_parameter.py        # a 역산 + C_time
│   │   ├── step_2_4_sanity_check.py       # 다중 차원 Sanity Check
│   │   └── step_2_5_initialization.py     # 모델 인스턴스화 + 연결
│   │
│   ├── engine/                        # Phase 3: Live Trading Engine
│   │   ├── __init__.py
│   │   ├── match_engine.py            # MatchEngine 메인 클래스
│   │   ├── state_machine.py           # Engine Phase + Event State 머신
│   │   ├── step_3_2_remaining_mu.py   # 잔여 기대 득점 (해석적 + P_grid)
│   │   ├── step_3_3_event_handler.py  # Preliminary/Confirmed 핸들러
│   │   ├── step_3_4_pricing.py        # 하이브리드 프라이싱 (해석적/MC)
│   │   ├── step_3_5_stoppage.py       # StoppageTimeManager
│   │   ├── mc_core.py                 # Numba JIT MC 시뮬레이션 코어
│   │   └── ob_freeze.py               # 3-Layer ob_freeze 로직
│   │
│   ├── trading/                       # Phase 4: Arbitrage & Execution
│   │   ├── __init__.py
│   │   ├── step_4_1_orderbook_sync.py # 호가창 동기화 + bet365 참조
│   │   ├── step_4_2_edge_detection.py # 방향별 EV + 3자 교차 검증
│   │   ├── step_4_3_position_sizing.py # Kelly + bet365 multiplier
│   │   ├── step_4_4_exit_logic.py     # 4개 청산 트리거
│   │   ├── step_4_5_order_execution.py # 주문 제출 + Rapid Entry
│   │   └── risk_manager.py            # 3-Layer 리스크 한도
│   │
│   ├── analytics/                     # Step 4.6: 사후 분석
│   │   ├── __init__.py
│   │   ├── daily.py                   # DailyAnalytics (매일 자정)
│   │   ├── metrics.py                 # 11개 지표 계산
│   │   ├── adaptive_params.py         # 7개 파라미터 적응적 조정
│   │   └── reports.py                 # 일일/주간 리포트 생성
│   │
│   ├── scheduler/                     # 자동 스케줄링
│   │   ├── __init__.py
│   │   └── main.py                    # MatchScheduler (24/7 실행)
│   │
│   ├── data/                          # 데이터 수집
│   │   ├── __init__.py
│   │   └── collector.py               # DataCollector (24/7 실행)
│   │
│   ├── alerts/                        # 알림 서비스
│   │   ├── __init__.py
│   │   ├── main.py                    # AlertService (24/7 실행)
│   │   ├── slack.py                   # Slack Webhook
│   │   └── telegram.py                # Telegram Bot
│   │
│   └── dashboard/                     # 대시보드
│       ├── __init__.py
│       ├── server.py                  # FastAPI + WebSocket 서버
│       ├── api/                       # REST API 엔드포인트
│       │   ├── __init__.py
│       │   ├── live.py                # Layer 1 데이터
│       │   ├── portfolio.py           # Layer 2 데이터
│       │   └── analytics.py           # Layer 3 데이터
│       │
│       └── frontend/                  # React 프론트엔드
│           ├── package.json
│           ├── src/
│           │   ├── App.jsx
│           │   ├── index.jsx
│           │   ├── components/
│           │   │   ├── Layout/
│           │   │   │   ├── Navbar.jsx
│           │   │   │   └── ModeBadge.jsx      # PAPER/LIVE 표시
│           │   │   │
│           │   │   ├── Layer1_LiveMatch/
│           │   │   │   ├── MatchPanel.jsx      # 경기별 패널 컨테이너
│           │   │   │   ├── MatchHeader.jsx     # 1A: 상태 헤더
│           │   │   │   ├── PriceChart.jsx      # 1B: P_true vs P_kalshi vs P_bet365 ⭐
│           │   │   │   ├── MuChart.jsx         # 1C: μ 감쇠 차트
│           │   │   │   ├── SignalPanel.jsx     # 1D: 시그널 + 포지션
│           │   │   │   ├── EventLog.jsx        # 1E: 이벤트 로그
│           │   │   │   └── SourceStatus.jsx    # 1F: 데이터 소스 상태
│           │   │   │
│           │   │   ├── Layer2_Portfolio/
│           │   │   │   ├── RiskDashboard.jsx   # 2A: 리스크 대시보드
│           │   │   │   ├── PositionTable.jsx   # 2B: 포지션 테이블
│           │   │   │   └── PnLTimeline.jsx     # 2C: P&L 타임라인
│           │   │   │
│           │   │   └── Layer3_Analytics/
│           │   │       ├── HealthDashboard.jsx # 3A: 건강 대시보드
│           │   │       ├── CalibrationPlot.jsx # 3B: Calibration Plot
│           │   │       ├── CumulativePnL.jsx   # 3C: 누적 P&L + Drawdown
│           │   │       ├── DirectionalAnalysis.jsx # 3D: 방향별 분석
│           │   │       ├── Bet365Effect.jsx    # 3E: bet365 검증 효과
│           │   │       ├── PrelimAccuracy.jsx  # 3F: Preliminary 정확도
│           │   │       └── ParamHistory.jsx    # 3G: 파라미터 히스토리
│           │   │
│           │   ├── hooks/
│           │   │   ├── useMatchStream.js       # WebSocket 실시간 데이터
│           │   │   ├── usePortfolio.js         # 포트폴리오 집계
│           │   │   └── useAnalytics.js         # 분석 API 호출
│           │   │
│           │   └── utils/
│           │       ├── formatters.js           # 가격/P&L 포맷
│           │       └── colors.js               # 상태별 색상 코드
│           │
│           └── build/                 # React 빌드 산출물 (gitignore)
│
├── scripts/
│   ├── setup_db.sql                   # PostgreSQL 스키마 초기화
│   ├── setup_systemd.sh               # systemd 서비스 등록
│   ├── deploy.sh                      # 배포 스크립트
│   ├── backup_db.sh                   # DB 백업
│   └── run_recalibration.py           # 수동 재학습 트리거
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py                    # pytest fixtures (mock Goalserve, mock Kalshi)
│   │
│   ├── unit/
│   │   ├── test_intervals.py          # Step 1.1 구간 분할
│   │   ├── test_Q_matrix.py           # Step 1.2 Q 추정
│   │   ├── test_nll.py                # Step 1.4 NLL 수학 검증
│   │   ├── test_a_parameter.py        # Step 2.3 역산
│   │   ├── test_remaining_mu.py       # Step 3.2 적분
│   │   ├── test_mc_core.py            # Step 3.4 MC 시뮬레이션
│   │   ├── test_edge_detection.py     # Step 4.2 EV + 방향별 P_cons
│   │   ├── test_kelly.py              # Step 4.3 Kelly
│   │   ├── test_exit_logic.py         # Step 4.4 청산 트리거
│   │   └── test_risk_limits.py        # 3-Layer 리스크
│   │
│   ├── integration/
│   │   ├── test_goalserve_client.py   # Goalserve API 연동
│   │   ├── test_kalshi_client.py      # Kalshi API 연동
│   │   ├── test_match_engine.py       # MatchEngine 전체 흐름
│   │   └── test_scheduler.py          # 스케줄러 스폰 로직
│   │
│   └── replay/
│       ├── replay_engine.py           # 과거 경기 리플레이 (백테스트)
│       └── test_replay.py             # 리플레이 기반 시스템 검증
│
├── docs/
│   ├── phase1_goalserve_v1.md         # Phase 1 설계 문서
│   ├── phase2_goalserve_v1.md         # Phase 2 설계 문서
│   ├── phase3_goalserve_v1.md         # Phase 3 설계 문서
│   ├── phase4_goalserve_v1.md         # Phase 4 설계 문서
│   ├── dashboard_design_v1.md         # 대시보드 설계 문서
│   └── implementation_blueprint.md    # 본 문서
│
└── logs/                              # 런타임 로그 (gitignore)
    ├── scheduler.log
    ├── engine/
    │   ├── ARS-CHE-20251018.log       # 경기별 엔진 로그
    │   └── ...
    ├── dashboard.log
    ├── alerts.log
    └── collector.log
```

---

## 구현 순서 로드맵

### Sprint 1: 기반 인프라 (1~2주)

```
목표: 프로세스가 뜨고, 데이터가 흐르고, 화면이 보인다

├── config/ 설정 파일 구조
├── src/common/ (config, logging, redis, db, types)
├── src/goalserve/client.py (REST API — Fixtures, Stats, Odds)
├── src/data/collector.py (과거 데이터 수집 시작)
├── scripts/setup_db.sql (PostgreSQL 스키마)
├── docker-compose.yml (Redis + PostgreSQL)
└── 검증: Goalserve API 호출 성공 + DB 적재 확인
```

### Sprint 2: Phase 1 — Offline Calibration (2~3주)

```
목표: 과거 데이터로 모델 파라미터를 학습한다

├── src/calibration/step_1_1_intervals.py
├── src/calibration/step_1_2_Q_matrix.py
├── src/calibration/features/ (Tier 1~4)
├── src/calibration/step_1_3_ml_prior.py
├── src/calibration/step_1_4_nll.py (PyTorch)
├── src/calibration/step_1_5_validation.py
├── tests/unit/test_intervals.py, test_nll.py, test_Q_matrix.py
└── 검증: Walk-Forward CV 통과, Brier Score < Pinnacle
```

### Sprint 3: Phase 2 + 3 코어 (2~3주)

```
목표: 경기 시작 전 초기화 + 실시간 μ/P_true 계산

├── src/prematch/ (Step 2.1~2.5)
├── src/engine/mc_core.py (Numba JIT)
├── src/engine/step_3_2_remaining_mu.py
├── src/engine/step_3_4_pricing.py
├── src/engine/state_machine.py
├── tests/unit/test_remaining_mu.py, test_mc_core.py
└── 검증: 과거 경기 리플레이로 P_true 산출 확인
```

### Sprint 4: Phase 3 이벤트 처리 + Phase 4 실행 (2~3주)

```
목표: 이벤트 처리 + 트레이딩 시그널 + 주문 (PAPER)

├── src/goalserve/live_odds_source.py (WebSocket)
├── src/goalserve/live_score_source.py (REST 폴링)
├── src/engine/step_3_3_event_handler.py
├── src/engine/ob_freeze.py
├── src/trading/ (Step 4.1~4.5)
├── src/kalshi/ (client, orderbook, execution)
├── tests/unit/test_edge_detection.py, test_kelly.py, test_exit_logic.py
└── 검증: 과거 경기 리플레이로 시그널 + 가상 주문 생성 확인
```

### Sprint 5: 스케줄러 + 24/7 자동화 (1~2주)

```
목표: 킨 순간부터 자동으로 돌아간다

├── src/scheduler/main.py
├── src/alerts/main.py
├── scripts/setup_systemd.sh
├── tests/integration/test_scheduler.py
└── 검증: 스케줄러가 오늘 경기 스캔 → 엔진 스폰 → PAPER 트레이딩
```

### Sprint 6: 대시보드 (2~3주)

```
목표: 실시간 모니터링 + 분석 뷰

├── src/dashboard/server.py
├── src/dashboard/frontend/ (React)
│   ├── Layer 1: PriceChart + MatchHeader + EventLog (Phase 0 필수)
│   ├── Layer 2: RiskDashboard + PositionTable + PnLTimeline
│   └── Layer 3: HealthDashboard (이후 Sprint에서 확장)
└── 검증: 브라우저에서 실시간 데이터 확인
```

### Sprint 7: 사후 분석 + 적응적 파라미터 (1~2주)

```
목표: Step 4.6 자동화 + 피드백 루프

├── src/analytics/ (daily, metrics, adaptive_params, reports)
├── CRON 2 설정
├── Slack/Telegram 알림 연동
└── 검증: 일일 리포트 자동 생성 + Slack 수신
```

### Sprint 8: Phase 0 PAPER 트레이딩 기간 (2~4주 운영)

```
목표: 실제 경기에서 PAPER 모드로 시스템 검증

├── 실제 경기에서 24/7 PAPER 운영
├── 가상 P&L 축적
├── Preliminary 정확도 측정
├── bet365 교차 검증 효과 측정
├── 버그 수정 + 안정화
└── 판정: Phase A 전환 기준 충족 여부
```

### Sprint 9: Phase A LIVE 전환 (1주 준비 + 운영)

```
목표: 실제 돈으로 보수적 트레이딩 시작

├── config/system.live.yaml 설정
├── trading_mode: "paper" → "live"
├── K_frac = 0.25, z = 1.645
├── Kalshi 실제 계좌 연동
└── 모니터링 강화: Drawdown 알림 임계값 10%
```

---

## 배포 체크리스트

### 서버 요구사항

| 항목 | 최소 사양 | 권장 사양 |
|------|----------|----------|
| CPU | 2 vCPU | 4 vCPU |
| RAM | 4 GB | 8 GB |
| Storage | 50 GB SSD | 100 GB SSD |
| OS | Ubuntu 22.04 LTS | Ubuntu 24.04 LTS |
| Network | 고정 IP (Goalserve 화이트리스트) | 고정 IP |
| 위치 | US East (Kalshi 서버 근접) | US East |

### 초기 배포 순서

```
1. 서버 프로비저닝 + 고정 IP 확보
2. Goalserve IP 화이트리스트 등록
3. Redis + PostgreSQL 설치
4. Python 3.11+ 환경 구성
5. 코드 배포 + 의존성 설치
6. DB 스키마 초기화 (scripts/setup_db.sql)
7. Phase 1 최초 실행 (data/parameters/production 생성)
8. systemd 서비스 등록 (scripts/setup_systemd.sh)
9. 대시보드 React 빌드 + 서빙
10. PAPER 모드로 시작 → Sprint 8 진입
```