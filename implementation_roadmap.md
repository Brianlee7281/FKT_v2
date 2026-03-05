# Implementation Roadmap — 태스크 단위 구현 순서

## 개요

Blueprint의 9개 Sprint를 실제 코딩 가능한 **태스크 단위**로 분해한다.
각 태스크는 의존성, 생성 파일, 검증 기준, 예상 소요를 포함한다.

### 읽는 법

```
[S1-T3] ← Sprint 1, Task 3
├── 의존: [S1-T1], [S1-T2]          ← 선행 태스크
├── 파일: src/common/db_client.py    ← 생성/수정 파일
├── 검증: pytest tests/unit/test_db.py PASS    ← 완료 기준
└── 소요: 0.5일                      ← 예상 소요
```

### 전체 의존성 그래프 (간략)

```
S1 (인프라) ──▶ S2 (Phase 1) ──▶ S3 (Phase 2+3 코어) ──▶ S4 (이벤트+실행)
                                                              │
S1 ──────────────────────────────────────▶ S5 (스케줄러) ◀────┘
                                              │
S1 ──────────────────▶ S6 (대시보드) ◀────────┤
                                              │
S4 ──────────────────▶ S7 (분석) ◀────────────┘
                           │
                           ▼
                    S8 (PAPER 운영) ──▶ S9 (LIVE 전환)
```

---

## Sprint 1: 기반 인프라 (1~2주)

> **목표:** 프로세스가 뜨고, 데이터가 흐르고, DB에 적재된다.

### [S1-T1] 프로젝트 스캐폴딩 + 설정 로더 (0.5일)

```
의존: 없음 (최초 태스크)
파일:
  ├── pyproject.toml                  # 의존성 정의 (poetry)
  ├── Makefile                        # make install, make test, make lint
  ├── config/system.yaml              # 기본 설정
  ├── config/system.paper.yaml        # PAPER 오버라이드
  ├── config/secrets.env.example      # API 키 템플릿
  ├── src/__init__.py
  ├── src/common/__init__.py
  └── src/common/config.py            # SystemConfig: YAML + env 로더

검증:
  • from src.common.config import load_config
  • config = load_config("config/system.yaml")
  • assert config.goalserve.api_key is not None

핵심 의존성:
  pyyaml, python-dotenv, pydantic (설정 검증)
```

### [S1-T2] 구조화 로깅 (0.5일)

```
의존: [S1-T1]
파일:
  └── src/common/logging.py           # structlog 기반 JSON 로거

검증:
  • log.info("test", match_id="123") → JSON 출력
  • 파일 + stdout 동시 출력

핵심 의존성:
  structlog
```

### [S1-T3] Redis 클라이언트 + Pub/Sub (1일)

```
의존: [S1-T1]
파일:
  ├── docker-compose.yml              # Redis + PostgreSQL 컨테이너
  └── src/common/redis_client.py      # 비동기 Redis Pub/Sub 래퍼

검증:
  • docker compose up -d
  • await redis.publish("test", "hello")
  • await redis.subscribe("test") → "hello" 수신
  • pytest tests/unit/test_redis.py

핵심 의존성:
  redis[hiredis], docker
```

### [S1-T4] PostgreSQL 클라이언트 + 스키마 (1일)

```
의존: [S1-T1], [S1-T3] (docker-compose)
파일:
  ├── scripts/setup_db.sql            # 전체 테이블 스키마 (Blueprint 참조)
  └── src/common/db_client.py         # asyncpg 래퍼 (CRUD 헬퍼)

검증:
  • psql -f scripts/setup_db.sql → 에러 없음
  • await db.upsert_match_job(...) → match_jobs 테이블에 행 삽입
  • pytest tests/unit/test_db.py

핵심 의존성:
  asyncpg, psycopg2 (마이그레이션용)
```

### [S1-T5] 공유 데이터 타입 정의 (0.5일)

```
의존: [S1-T1]
파일:
  └── src/common/types.py

내용:
  @dataclass NormalizedEvent       # type, source, confidence, score, team, ...
  @dataclass Signal                # direction, EV, P_cons, P_kalshi, alignment_status, ...
  @dataclass IntervalRecord        # match_id, t_start, t_end, state_X, delta_S, ...
  @dataclass TradeLog              # Phase 4 v2 전체 필드
  @dataclass Position              # direction, entry_price, quantity, ...
  @dataclass SanityResult          # verdict, delta_match_winner, delta_over_under
  @dataclass MarketAlignment       # status, kelly_multiplier
  enum TradingMode                 # PAPER, LIVE
  enum EnginePhase                 # FIRST_HALF, HALFTIME, SECOND_HALF, FINISHED
  enum EventState                  # IDLE, PRELIMINARY_DETECTED, CONFIRMED

검증:
  • 모든 타입 import 성공
  • dataclass 직렬화/역직렬화 (json.dumps/loads)
```

### [S1-T6] Goalserve REST 클라이언트 (2일)

```
의존: [S1-T1], [S1-T2]
파일:
  ├── src/goalserve/__init__.py
  ├── src/goalserve/client.py         # GoalserveClient (Fixtures, Stats, Odds)
  └── src/goalserve/parsers.py        # JSON → 내부 타입 변환

구현할 메서드:
  • get_fixtures(league_id, date) → List[MatchFixture]
  • get_match_stats(match_id) → MatchStats (player_stats 포함)
  • get_odds(league_id, date) → List[MatchOdds]
  • get_live_scores() → List[LiveMatch]

검증:
  • Goalserve Trial API로 실제 호출
  • fixtures = await client.get_fixtures("1204", "01.03.2025")
  • assert len(fixtures) > 0
  • pytest tests/integration/test_goalserve_client.py

주의:
  • IP 화이트리스트 등록 선행 필요
  • Rate limit 확인 (Trial 제한)
```

### [S1-T7] 데이터 수집기 — 과거 데이터 적재 시작 (1.5일)

```
의존: [S1-T4], [S1-T6]
파일:
  ├── src/data/__init__.py
  └── src/data/collector.py           # DataCollector

구현:
  • collect_historical_season(league_id, season) → DB에 historical_matches 적재
  • collect_yesterday_results() → 어제 경기 결과 적재
  • verify_data_integrity() → 무결성 검증

검증:
  • EPL 2023-24 시즌 전체 경기 적재
  • SELECT COUNT(*) FROM historical_matches WHERE league_id='1204' → 380
  • player_stats JSONB가 비어있지 않은 경기 비율 확인

중요:
  • 이 태스크 완료 후 바로 과거 5시즌 데이터 수집을 백그라운드로 시작
  • Sprint 2 시작 전까지 최소 2시즌 적재 목표
```

### Sprint 1 완료 기준

```
□ docker compose up → Redis + PostgreSQL 정상 기동
□ Goalserve API 호출 성공 (Fixtures, Stats, Odds)
□ 최소 1시즌 과거 데이터 DB 적재 완료
□ Redis Pub/Sub 메시지 송수신 확인
□ 모든 단위 테스트 통과
```

---

## Sprint 2: Phase 1 — Offline Calibration (2~3주)

> **목표:** 과거 데이터로 모델 파라미터(b, γ, δ, Q)를 학습한다.

### [S2-T1] Step 1.1: 구간 분할 (2일)

```
의존: [S1-T5], [S1-T7] (데이터 적재 완료)
파일:
  ├── src/calibration/__init__.py
  └── src/calibration/step_1_1_intervals.py

구현:
  • build_intervals_from_goalserve(match_data) → List[IntervalRecord]
  • parse_minute(minute_str, extra_min_str) → float
  • resolve_scoring_team(goal_event, recorded_team) → str (자책골 반전)
  • VAR 취소골 필터링 (var_cancelled == "True")
  • 하프타임 구간 처리 (is_halftime 플래그)
  • 전체 DB에서 배치 변환: build_all_intervals(db) → List[IntervalRecord]

검증:
  • 2022 월드컵 결승 수동 검증 (문서의 데이터 변환 예시와 정확히 일치)
  • VAR 취소골이 있는 경기에서 올바르게 제외되는지
  • 자책골의 득점 팀 반전 검증
  • pytest tests/unit/test_intervals.py
    - test_basic_interval_split
    - test_var_cancelled_goal_excluded
    - test_own_goal_team_reversal
    - test_halftime_excluded
    - test_added_time_T_m
```

### [S2-T2] Step 1.2: Q 행렬 추정 (1일)

```
의존: [S2-T1]
파일:
  └── src/calibration/step_1_2_Q_matrix.py

구현:
  • reconstruct_markov_path(match_data) → List[(minute, state)]
  • estimate_Q_matrix(intervals, config) → np.ndarray (4×4)
  • 상태별 체류 시간 계산 (하프타임 제외)
  • 희소 상태 처리 (가산 가정: q_{1→3} ≈ q_{0→2})
  • Q_off 정규화: normalize_Q_off(Q) → np.ndarray (4×4)

검증:
  • Q 대각 성분 = -Σ 비대각 (행 합 = 0)
  • 모든 비대각 성분 ≥ 0
  • Q_off_normalized 각 행 합 ≈ 1.0
  • pytest tests/unit/test_Q_matrix.py
```

### [S2-T3] Step 1.3: 피처 엔지니어링 — Tier 1~4 (3일)

```
의존: [S1-T7] (데이터 적재)
파일:
  ├── src/calibration/features/__init__.py
  ├── src/calibration/features/tier1_team.py       # 팀 롤링 (xG, shots, possession)
  ├── src/calibration/features/tier2_player.py     # 선수 집계 (rating, keyPasses)
  ├── src/calibration/features/tier3_odds.py       # 배당 (Pinnacle, 시장 평균)
  └── src/calibration/features/tier4_context.py    # H/A, 휴식일, H2H

각 Tier별:
  • build_tier{N}_features(match_id, db) → dict
  • 결측값 처리 (빈 문자열, None → NaN)
  • minutes_played < 10 필터링 (Tier 2)

검증:
  • 특정 경기의 피처 벡터를 수동 계산과 비교
  • NaN 비율 확인 (Tier 2에서 선수 데이터 가용성)
  • Tier 3: Pinnacle이 없는 경기에서 시장 평균으로 fallback
  • pytest tests/unit/test_features.py
```

### [S2-T4] Step 1.3: XGBoost Poisson 학습 (2일)

```
의존: [S2-T3]
파일:
  └── src/calibration/step_1_3_ml_prior.py

구현:
  • build_training_dataset(db, config) → X, y_home, y_away
  • train_poisson_model(X, y) → xgb.Booster
  • select_features(model) → feature_mask (누적 중요도 95%)
  • save_model(model, path), save_feature_mask(mask, path)
  • predict_expected_goals(X_match) → (μ_H, μ_A)

검증:
  • 학습 세트 Poisson deviance 감소 확인
  • feature_mask.json에 10~30개 피처 선택됨
  • predict 출력이 합리적 범위 (0.5 < μ < 4.0)
  • pytest tests/unit/test_ml_prior.py
```

### [S2-T5] Step 1.4: NLL 최적화 (4일) ⭐ 핵심 태스크

```
의존: [S2-T1], [S2-T2], [S2-T4]
파일:
  └── src/calibration/step_1_4_nll.py

구현:
  • NLLModel(nn.Module): forward() → loss
    - 학습 파라미터: a_H[M], a_A[M], b[6], γ^H[2], γ^A[2], δ_H[4], δ_A[4]
    - 홈/어웨이 골 분리 NLL
    - 자책골 점 이벤트 제외
    - ML Prior 정규화 + L2 정규화
  • 파라미터 클램핑 (b, γ^H, γ^A, δ_H, δ_A 범위)
  • Multi-start 최적화 (5~10 시드)
  • 2단계: Adam 1000 epochs → L-BFGS fine-tuning
  • joint_nll_optimization(intervals, ml_model, Q, config) → params

검증:
  • NLL이 수렴하는지 (loss curve 단조 감소)
  • γ^H_1 < 0, γ^H_2 > 0, γ^A_1 > 0, γ^A_2 < 0 (부호 검증)
  • δ_H(+1) < 0, δ_A(+1) > 0 (부호 검증)
  • b의 전후반 비중이 실제 슈팅 비중과 ±10%
  • Multi-start에서 최저 NLL 시드가 일관되게 유사한 파라미터
  • pytest tests/unit/test_nll.py
    - test_nll_gradient_check (수치 미분 vs 자동 미분)
    - test_nll_convergence
    - test_gamma_sign_constraints
    - test_delta_sign_constraints
    - test_own_goal_excluded_from_point_events
```

### [S2-T6] Step 1.5: Validation (2일)

```
의존: [S2-T5]
파일:
  └── src/calibration/step_1_5_validation.py

구현:
  • walk_forward_cv(intervals, config) → ValidationReport
    - 3-Fold 시계열 CV
    - 각 Fold에서 Step 1.3 + 1.4 재실행
  • compute_brier_score(P_pred, outcomes) → float
  • compute_pinnacle_baseline(odds_db) → float
  • compute_calibration_data(P_pred, outcomes, n_bins=10) → dict
  • multi_market_validation(μ_H, μ_A, odds_db) → dict (1X2 + O/U + BTTS)
  • gamma_sign_check(params) → bool
  • delta_lrt(params_with_delta, params_without) → (LR_stat, p_value)
  • b_half_ratio_check(b, stats_db) → float (discrepancy)

검증:
  • ΔBS < 0 (모델이 Pinnacle보다 나음)
  • Calibration plot이 대각선 ±5% 이내
  • 3개 Fold 모두에서 시뮬레이션 수익 양수
  • 다중 마켓 (1X2, O/U, BTTS) 모두 시장 대비 개선
  • Go/No-Go 판정 자동화
```

### [S2-T7] Phase 1 오케스트레이터 + 파라미터 저장 (1일)

```
의존: [S2-T6]
파일:
  └── src/calibration/recalibrate.py

구현:
  • Recalibrator.run(trigger_reason) → 전체 파이프라인
  • deploy_parameters(params, feature_mask, Q) → version string
  • 심볼릭 링크 업데이트 (data/parameters/production)
  • Redis hot-reload 시그널 발송

검증:
  • python -m src.calibration.recalibrate --reason "initial"
  • data/parameters/production/params.json 존재
  • data/parameters/production/xgboost.xgb 존재
  • data/parameters/production/validation_report.json의 verdict == "GO"
```

### Sprint 2 완료 기준

```
□ 최소 3시즌 데이터로 Step 1.1~1.5 전체 파이프라인 실행 성공
□ Walk-Forward CV 3개 Fold 모두 통과
□ ΔBS < 0 (Pinnacle 대비 개선)
□ γ 부호 4개 모두 올바름
□ data/parameters/production/ 에 프로덕션 파라미터 저장
□ 모든 단위 테스트 통과 (test_intervals, test_Q, test_nll 등)
```

---

## Sprint 3: Phase 2 + 3 코어 (2~3주)

> **목표:** Pre-Match 초기화 + 실시간 μ/P_true 계산이 동작한다.

### [S3-T1] Step 2.1~2.2: 데이터 수집 + 피처 선택 (2일)

```
의존: [S2-T3] (피처 엔지니어링), [S2-T7] (feature_mask)
파일:
  ├── src/prematch/__init__.py
  ├── src/prematch/step_2_1_data_collection.py
  └── src/prematch/step_2_2_feature_selection.py

구현:
  • collect_prematch_data(match_id, config) → PreMatchData
    - 라인업 (Live Game Stats teams.{team})
    - 선수 롤링 (과거 player_stats)
    - 팀 롤링 (과거 stats.{team})
    - 배당 (Pregame Odds)
    - 컨텍스트 (휴식일, H2H)
  • apply_feature_mask(prematch, feature_mask, median_values) → np.ndarray

검증:
  • 특정 경기의 PreMatchData 내용 수동 확인
  • feature_mask 적용 후 벡터 차원이 Phase 1과 동일
  • 결측 시 median 대체 작동
```

### [S3-T2] Step 2.3: a 파라미터 역산 (0.5일)

```
의존: [S3-T1], [S2-T7] (XGBoost 모델)
파일:
  └── src/prematch/step_2_3_a_parameter.py

구현:
  • predict_expected_goals(X_match, model_path) → (μ_H, μ_A)
  • compute_C_time(b, E_alpha1, E_alpha2) → float
  • compute_a_parameters(μ_H, μ_A, C_time) → (a_H, a_A)
    - a = ln(μ) - ln(C_time)

검증:
  • a_H, a_A가 합리적 범위 (-2 < a < 1)
  • exp(a_H) × C_time ≈ μ_H (역산 검증)
  • pytest tests/unit/test_a_parameter.py
```

### [S3-T3] Step 2.4: Sanity Check (1일)

```
의존: [S3-T2]
파일:
  └── src/prematch/step_2_4_sanity_check.py

구현:
  • primary_sanity_check(μ_H, μ_A, pinnacle, market_avg) → str
  • secondary_sanity_check(μ_H, μ_A, ou_odds) → dict
  • combined_sanity_check(...) → SanityResult

검증:
  • 다양한 괴리도에서 GO/GO_WITH_CAUTION/HOLD/SKIP 판정
  • O/U 교차 검증이 "합은 맞지만 비율이 잘못된" 경우 감지
  • pytest tests/unit/test_sanity.py
```

### [S3-T4] Step 2.5: 모델 인스턴스화 (1일)

```
의존: [S3-T2], [S2-T2] (Q 행렬)
파일:
  └── src/prematch/step_2_5_initialization.py

구현:
  • initialize_model(match_id, a_H, a_A, C_time, config) → LiveFootballQuantModel
    - P_grid[0..100] 사전 계산 (scipy.linalg.expm)
    - P_fine_grid[0..30] 10초 단위 (경기 종료 직전용)
    - Q_off_normalized 정규화
    - 초기 상태 설정 (t=0, X=0, S=(0,0), ΔS=0)

검증:
  • P_grid[0] == identity matrix
  • P_grid[90]의 행 합 ≈ 1.0
  • Q_off_normalized 각 행 합 ≈ 1.0
```

### [S3-T5] Numba MC 코어 (2일) ⭐ 성능 핵심

```
의존: [S1-T5] (types)
파일:
  └── src/engine/mc_core.py

구현:
  • @njit mc_simulate_remaining(...) → np.ndarray (N, 2)
    - 팀별 γ (gamma_H, gamma_A)
    - 정규화 Q_off
    - 기저함수 경계 처리
    - δ(ΔS) 인덱스 변환
  • Numba 워밍업 함수

검증:
  • 알려진 단순 케이스 수동 검증 (X=0, ΔS=0, 1구간)
    → Poisson 해석적 결과와 MC 결과 비교 (N=100000, 상대오차 < 1%)
  • 성능: N=50000에서 < 1ms (Numba 컴파일 후)
  • 결정론적 시드: 동일 입력 → 동일 출력
  • pytest tests/unit/test_mc_core.py
    - test_mc_vs_analytical_poisson
    - test_mc_deterministic_seed
    - test_mc_performance_benchmark
    - test_mc_red_card_transition
    - test_mc_delta_score_effect
```

### [S3-T6] Step 3.2: 잔여 기대 득점 (1.5일)

```
의존: [S3-T4] (P_grid), [S3-T5] (MC 코어)
파일:
  ├── src/engine/__init__.py
  └── src/engine/step_3_2_remaining_mu.py

구현:
  • compute_remaining_mu(model) → (μ_H, μ_A)
    - Piecewise 구간 분해
    - P_grid 조회 (일반) / P_fine_grid (종료 5분 전)
    - 마르코프 변조 적분
  • analytical_remaining_mu(model, delta_S) → (μ_H, μ_A) (해석적)

검증:
  • t=0, X=0, ΔS=0에서 μ_H + μ_A ≈ μ_total (Phase 2 예측과 일치)
  • t→T에서 μ→0 수렴
  • 레드카드 상태에서 μ_H, μ_A가 반대 방향으로 변동
  • pytest tests/unit/test_remaining_mu.py
```

### [S3-T7] Step 3.4: 하이브리드 프라이싱 (1.5일)

```
의존: [S3-T5], [S3-T6]
파일:
  └── src/engine/step_3_4_pricing.py

구현:
  • analytical_pricing(μ_H, μ_A, S) → dict  (Poisson/Skellam)
    - Over/Under: G 정의 + edge case (G > N → P=1)
    - Match Odds: 스켈람 분포
  • aggregate_markets(final_scores, S) → dict (MC 결과 집계)
  • compute_mc_stderr(P_true, N) → float
  • step_3_4_async(model, μ_H, μ_A) → (P_true, σ_MC)
    - 해석적/MC 분기 (DELTA_SIGNIFICANT)
    - Executor 디커플링
    - Stale 체크 + PRELIMINARY 체크

검증:
  • X=0, ΔS=0에서 해석적 vs MC 결과 차이 < 1%
  • σ_MC가 N 증가에 따라 감소 (√N 비례)
  • pytest tests/unit/test_pricing.py
```

### [S3-T8] 리플레이 엔진 — 과거 경기 재생 (2일)

```
의존: [S3-T6], [S3-T7]
파일:
  ├── tests/replay/replay_engine.py
  └── tests/replay/test_replay.py

구현:
  • ReplayEngine: 과거 경기의 이벤트를 시간순으로 재생하면서
    Step 3.2 + 3.4를 실행하고, 매 분마다 P_true를 기록
  • 결과를 CSV/JSON으로 저장

검증:
  • EPL 2023-24 시즌 10경기에서 리플레이 실행
  • P_true가 합리적 범위 (0.01~0.99)
  • 골 발생 시 P_true 점프 확인
  • 시간 경과에 따른 P_true 수렴 (경기 종료 직전 → 0 or 1에 가까움)

중요: 이 리플레이 엔진이 이후 Sprint에서 End-to-End 검증의 핵심 도구가 됨
```

### Sprint 3 완료 기준

```
□ Phase 2 (Step 2.1~2.5) 전체 파이프라인 실행 성공
□ Numba MC 코어: N=50000에서 < 1ms
□ 해석적 vs MC P_true 차이 < 1% (X=0, ΔS=0)
□ 리플레이 엔진으로 과거 10경기 P_true 산출 성공
□ 모든 단위 테스트 통과
```

---

## Sprint 4: Phase 3 이벤트 처리 + Phase 4 실행 (2~3주)

> **목표:** 실시간 이벤트 처리 + 트레이딩 시그널 + 주문 실행(PAPER)이 동작한다.

### [S4-T1] 엔진 상태 머신 (1일)

```
의존: [S1-T5]
파일:
  └── src/engine/state_machine.py

구현:
  • EnginePhase: FIRST_HALF → HALFTIME → SECOND_HALF → FINISHED
  • EventState: IDLE → PRELIMINARY_DETECTED → CONFIRMED → COOLDOWN → IDLE
  • 상태 전이 규칙 + 무효 전이 차단

검증:
  • 유효 전이: IDLE → PRELIMINARY → CONFIRMED (IDLE)
  • 무효 전이: PRELIMINARY → PRELIMINARY (무시)
  • 타임아웃: PRELIMINARY → IDLE (10초)
```

### [S4-T2] Goalserve Live Odds WebSocket 소스 (2일)

```
의존: [S1-T6], [S4-T1]
파일:
  └── src/goalserve/live_odds_source.py

구현:
  • GoalserveLiveOddsSource(EventSource)
    - WebSocket 연결 + 재연결 로직
    - score 변동 감지 → goal_detected / score_rollback
    - 배당 급변 감지 → odds_spike
    - period 변경 감지 → period_change
    - minute > 45/90 → stoppage_entered

검증:
  • Mock WebSocket으로 단위 테스트
  • 실제 Goalserve Trial로 통합 테스트 (라이브 경기 있을 때)
```

### [S4-T3] Goalserve Live Score REST 소스 (1.5일)

```
의존: [S1-T6], [S4-T1]
파일:
  └── src/goalserve/live_score_source.py

구현:
  • GoalserveLiveScoreSource(EventSource)
    - 3초 폴링 + 이전 상태와 diff
    - 골 확정 (team, scorer_id, var_cancelled)
    - 레드카드 확정
    - period 변경

검증:
  • Mock 데이터로 diff 로직 단위 테스트
  • 연속 폴링에서 중복 이벤트 방지
```

### [S4-T4] Step 3.3: 이벤트 핸들러 — Preliminary + Confirmed (2일)

```
의존: [S4-T1], [S4-T2], [S4-T3], [S3-T6]
파일:
  └── src/engine/step_3_3_event_handler.py

구현:
  • handle_preliminary_goal(model, event)
    - ob_freeze + PRELIMINARY 상태 + μ 사전 계산
  • handle_score_rollback(model, event)
    - VAR 취소 → 상태 롤백
  • handle_confirmed_goal(model, event)
    - VAR 확인 → S, ΔS, δ 업데이트 + cooldown
    - 사전 계산 재사용 (preliminary_cache)
  • handle_confirmed_red_card(model, event)
    - X 전이 + γ^H, γ^A 변경 + μ 재계산
  • handle_odds_spike(model, event)
  • handle_period_change(model, event)
  • precompute_preliminary_mu(model, delta_S, scoring_team) (async)

검증:
  • 골 시나리오: IDLE → PRELIMINARY → CONFIRMED → COOLDOWN → IDLE
  • VAR 취소: IDLE → PRELIMINARY → score_rollback → IDLE
  • 레드카드: odds_spike(ob_freeze) → red_card(confirmed) → IDLE
  • pytest tests/unit/test_event_handler.py
```

### [S4-T5] ob_freeze 3-Layer 로직 (1일)

```
의존: [S4-T1]
파일:
  └── src/engine/ob_freeze.py

구현:
  • check_ob_freeze_release(model) → 해제 조건 3가지
  • 쿨다운 타이머 (15초)

검증:
  • 해제 조건 1: cooldown이 이어받으면 ob_freeze 해제
  • 해제 조건 2: 3틱 안정화
  • 해제 조건 3: 10초 타임아웃
```

### [S4-T6] Step 3.5: 추가시간 관리자 (0.5일)

```
의존: [S4-T2], [S4-T3]
파일:
  └── src/engine/step_3_5_stoppage.py

구현:
  • StoppageTimeManager
    - update_from_live_odds(minute, period) → T
    - update_from_live_score(minute, period) → T
    - Phase B: T_game 유지 (전반 추가시간)
    - Phase C: T 롤링 (후반 추가시간)
    - 이중 소스 교차 검증 (2분 이상 차이 시 경고)

검증:
  • minute=92, period="2nd Half" → T = 93.5
  • minute=46, period="1st Half" → T = T_exp (변경 없음)
```

### [S4-T7] Kalshi 클라이언트 + OrderBookSync (2일)

```
의존: [S1-T5]
파일:
  ├── src/kalshi/__init__.py
  ├── src/kalshi/client.py              # REST + WebSocket
  └── src/kalshi/orderbook.py           # OrderBookSync

구현:
  • KalshiClient
    - WebSocket 연결 (호가 수신)
    - REST: submit_order, cancel_order, get_positions, get_balance
  • OrderBookSync
    - update_kalshi(orderbook)
    - compute_vwap_buy(target_qty) → float
    - compute_vwap_sell(target_qty) → float
    - update_bet365(live_odds_markets)

검증:
  • Kalshi Demo/Paper API로 연동 테스트
  • VWAP 계산: 수동 검증 (3호가 레벨 예시)
  • pytest tests/integration/test_kalshi_client.py
```

### [S4-T8] Step 4.2: Edge Detection — 2-pass VWAP + 시장 정합성 (2일)

```
의존: [S4-T7], [S3-T7]
파일:
  └── src/trading/step_4_2_edge_detection.py

구현:
  • compute_conservative_P(P_true, σ_MC, direction, z) → float
  • compute_signal_with_vwap(...) → Signal (2-pass)
  • check_market_alignment(P_cons, P_kalshi, P_bet365, direction) → MarketAlignment
  • generate_signal(...) → Signal

검증:
  • Buy Yes: P_cons = P_true - z*σ
  • Buy No: P_cons = P_true + z*σ (방향별 보정)
  • VWAP > best ask일 때 final_EV < rough_EV
  • VWAP 반영 후 엣지 소멸 시 HOLD 반환
  • ALIGNED → mult 0.8, DIVERGENT → mult 0.5
  • pytest tests/unit/test_edge_detection.py
    - test_buy_yes_conservative_P
    - test_buy_no_conservative_P
    - test_vwap_reduces_EV
    - test_vwap_kills_thin_edge
    - test_alignment_status
```

### [S4-T9] Step 4.3: Kelly + 리스크 한도 (1일)

```
의존: [S4-T8]
파일:
  ├── src/trading/step_4_3_position_sizing.py
  └── src/trading/risk_manager.py

구현:
  • compute_kelly(signal, c, K_frac) → float
    - 방향별 W/L
    - alignment_status multiplier
  • apply_risk_limits(f_invest, match_id, bankroll) → float
    - 3-Layer (3%/5%/20%)
    - pro-rata 축소

검증:
  • Buy Yes Kelly vs Buy No Kelly (같은 EV에서 다른 f)
  • Layer 1 초과 시 클램핑
  • Layer 2 초과 시 해당 경기 차단
  • Layer 3 초과 시 전체 차단
  • pytest tests/unit/test_kelly.py, test_risk_limits.py
```

### [S4-T10] Step 4.4: 청산 로직 — 방향별 수식 (2일)

```
의존: [S4-T8]
파일:
  └── src/trading/step_4_4_exit_logic.py

구현:
  • check_edge_decay(position, P_true, σ_MC, P_kalshi_bid, c, z)
  • check_edge_reversal(position, P_true, σ_MC, P_kalshi_bid, z)
    - Buy No: P_cons > P_kalshi_bid + θ (v2 수정)
  • check_expiry_eval(position, P_true, σ_MC, P_kalshi_bid, c, z, t, T)
    - 방향별 E_hold (v2 수정)
    - 방향별 E_exit
  • check_bet365_divergence(position, P_bet365)
    - Buy No: P_bet365 > entry + 0.05 (v2 수정)
  • evaluate_exit(...) → Optional[ExitSignal]

검증:
  • Buy No Edge Reversal: bid=0.40 → 임계값 0.42 (NOT 0.62)
  • Buy No Expiry: entry=0.40, P_cons=0.35 → E_hold 양수 (보유 유리)
  • Buy No Divergence: entry=0.40 → 임계값 0.45 (NOT 0.65)
  • pytest tests/unit/test_exit_logic.py
    - test_buy_no_edge_reversal_threshold
    - test_buy_no_expiry_hold_vs_exit
    - test_buy_no_divergence_threshold
    - test_buy_yes_all_triggers (기존 검증)
```

### [S4-T11] Step 4.5: 주문 실행 — PAPER + LIVE (1.5일)

```
의존: [S4-T7], [S4-T9]
파일:
  └── src/kalshi/execution.py

구현:
  • ExecutionLayer (모드 분기)
  • PaperExecutionLayer
    - VWAP 기반 체결가 (v2)
    - 1 tick 슬리피지 가산
    - 부분 체결 시뮬레이션
  • LiveExecutionLayer
    - Kalshi REST 주문 제출
    - 5초 체결 대기 + 미체결 취소
  • post_event_rapid_entry (조건부, 초기 비활성)
    - VAR 안전 대기 5초 (v2)
    - P_cons 보정 (v2)

검증:
  • Paper: VWAP > best ask 확인 (슬리피지 반영)
  • Paper: 호가 부족 시 부분 체결
  • Live: Mock Kalshi API로 주문 플로우 테스트
```

### [S4-T12] Step 4.6: 정산 P&L — 방향별 (0.5일)

```
의존: [S1-T5]
파일:
  └── src/analytics/metrics.py (정산 부분만 먼저)

구현:
  • compute_realized_pnl(position, settlement, fee_rate) → float
    - Buy Yes: (settlement - entry) × qty - fee
    - Buy No: (entry - settlement) × qty - fee  (v2 수정)

검증:
  • Buy No, entry=0.40, settlement=0.00 → PnL 양수 (No 승리)
  • Buy No, entry=0.40, settlement=1.00 → PnL 음수 (No 패배)
  • pytest tests/unit/test_settlement.py
```

### [S4-T13] MatchEngine 통합 — Phase 2~4 결합 (2일)

```
의존: [S4-T1]~[S4-T12] 전부
파일:
  └── src/engine/match_engine.py

구현:
  • MatchEngine 클래스
    - run_prematch() → SanityResult
    - run_live() → 3개 코루틴 gather
    - _tick_loop(): Step 3.2 → 3.4 → 4.2~4.5
    - _live_odds_listener(): 이벤트 핸들링
    - _live_score_poller(): 이벤트 핸들링
    - _publish_state_snapshot(): Redis 발행
    - _emergency_shutdown(): 안전 종료

검증:
  • 리플레이 엔진으로 과거 경기 End-to-End 실행
  • Phase 2 → 3 → 4 전체 파이프라인 동작
  • 골 이벤트 → PRELIMINARY → CONFIRMED → 시그널 → 가상 주문
  • tests/integration/test_match_engine.py
```

### Sprint 4 완료 기준

```
□ MatchEngine이 리플레이 모드로 과거 경기 전체 파이프라인 실행 성공
□ 3-Layer 감지: Live Odds WS + Kalshi WS + Live Score REST 동시 동작
□ PRELIMINARY → CONFIRMED 2단계 처리 정상 작동
□ Phase 4 v2 모든 수정 반영 (방향별 수식, VWAP, Paper 슬리피지)
□ 가상 주문(PAPER) 생성 + 정산 P&L 계산 정상
□ 모든 단위 테스트 + 통합 테스트 통과
```

---

## Sprint 5: 스케줄러 + 24/7 자동화 (1~2주)

> **목표:** 킨 순간부터 자동으로 돌아간다.

### [S5-T1] MatchScheduler 코어 (2일)

```
의존: [S4-T13]
파일:
  └── src/scheduler/main.py

구현:
  • scan_today_matches() → DB에 match_jobs 생성
  • spawn_engine(match_id) → MatchEngine 생성 + Phase 2 실행
  • monitor_engines() → 건강 체크 (10초 간격)
  • handle_match_finished() → 정리 + 로그 기록
  • 매일 06:00 스케줄 스캔 + 매 5분 갱신
```

### [S5-T2] systemd 서비스 등록 (0.5일)

```
의존: [S5-T1]
파일:
  ├── scripts/setup_systemd.sh
  ├── /etc/systemd/system/kalshi-scheduler.service
  ├── /etc/systemd/system/kalshi-dashboard.service
  ├── /etc/systemd/system/kalshi-alerts.service
  └── /etc/systemd/system/kalshi-collector.service

검증:
  • systemctl start kalshi-scheduler → 프로세스 기동
  • kill 후 5초 내 자동 재시작
  • 서버 리부트 후 전 서비스 자동 시작
```

### [S5-T3] 알림 서비스 (1.5일)

```
의존: [S1-T3] (Redis)
파일:
  ├── src/alerts/__init__.py
  ├── src/alerts/main.py
  ├── src/alerts/slack.py
  └── src/alerts/telegram.py

구현:
  • AlertService: Redis 구독 → 자동 알림
  • Slack Webhook + Telegram Bot 연동
  • 상태 기반 자동 알림 (Drawdown, PRELIMINARY 30초+, 소스 장애)

검증:
  • Redis에 alert 발행 → Slack에 메시지 수신
  • CRITICAL 이벤트 → Telegram에도 수신
```

### Sprint 5 완료 기준

```
□ 스케줄러가 오늘 경기 자동 스캔 + 엔진 자동 스폰
□ systemd로 프로세스 자동 시작 + 크래시 복구
□ Slack 알림 수신 확인
□ kill -9 scheduler → 5초 후 자동 재시작 확인
```

---

## Sprint 6: 대시보드 (2~3주)

> **목표:** 실시간 모니터링 UI가 브라우저에서 동작한다.

### [S6-T1] FastAPI WebSocket 서버 (1.5일)

```
의존: [S1-T3]
파일:
  ├── src/dashboard/__init__.py
  ├── src/dashboard/server.py
  └── src/dashboard/api/live.py

구현:
  • /ws/live/{match_id} — 경기별 실시간 스트림
  • /ws/portfolio — 전체 포트폴리오 스트림
  • Redis Pub/Sub → WebSocket bridge
```

### [S6-T2] React 프론트엔드 스캐폴딩 (1일)

```
의존: 없음 (프론트엔드 독립)
파일:
  ├── src/dashboard/frontend/package.json
  ├── src/dashboard/frontend/src/App.jsx
  ├── src/dashboard/frontend/src/index.jsx
  ├── src/dashboard/frontend/src/hooks/useMatchStream.js
  └── src/dashboard/frontend/src/utils/formatters.js

구현:
  • Create React App + Tailwind CSS + Recharts
  • useMatchStream hook (WebSocket 연결 + 자동 재연결)
```

### [S6-T3] Layer 1: PriceChart ⭐ (2일)

```
의존: [S6-T1], [S6-T2]
파일:
  └── src/dashboard/frontend/src/components/Layer1_LiveMatch/PriceChart.jsx

구현:
  • P_true (파랑) vs P_kalshi (빨강) vs P_bet365 (초록) 실시간 차트
  • 엣지 영역 음영
  • 이벤트 마커 (골, 레드카드, 하프타임)
  • PRELIMINARY 점선 → CONFIRMED 실선 전환
  • 시장 탭 (Over 2.5, Home Win 등)

검증:
  • 브라우저에서 실시간 데이터 수신 확인
  • 3개 라인이 정상적으로 갱신
  • 이벤트 마커가 올바른 시점에 표시
```

### [S6-T4] Layer 1: 나머지 패널 (2일)

```
의존: [S6-T3]
파일:
  ├── MatchHeader.jsx        # 1A: 상태 헤더 + 색상 코딩
  ├── MuChart.jsx            # 1C: μ 감쇠 차트
  ├── SignalPanel.jsx        # 1D: 시그널 + 포지션
  ├── EventLog.jsx           # 1E: 이벤트 로그 (색상 코딩)
  └── SourceStatus.jsx       # 1F: 데이터 소스 상태
```

### [S6-T5] Layer 2: 포트폴리오 뷰 (2일)

```
의존: [S6-T1]
파일:
  ├── src/dashboard/api/portfolio.py
  ├── RiskDashboard.jsx      # 2A: 3-Layer 리스크 게이지
  ├── PositionTable.jsx      # 2B: 포지션 테이블
  └── PnLTimeline.jsx        # 2C: P&L 타임라인
```

### [S6-T6] Layer 3: Analytics 뷰 — Phase 0 필수 (2일)

```
의존: [S6-T1], [S4-T12]
파일:
  ├── src/dashboard/api/analytics.py
  ├── HealthDashboard.jsx    # 3A: 7개 게이지
  └── CumulativePnL.jsx      # 3C: 누적 P&L + Drawdown
```

### Sprint 6 완료 기준

```
□ 브라우저에서 실시간 P_true/P_kalshi/P_bet365 차트 확인
□ 경기 상태 색상 코딩 작동 (PRELIMINARY → 노랑 등)
□ 포트폴리오 리스크 게이지 작동
□ PAPER/LIVE 모드 배지 표시
```

---

## Sprint 7: 사후 분석 + 적응적 파라미터 (1~2주)

> **목표:** Step 4.6 자동화 + 피드백 루프가 동작한다.

### [S7-T1] 11개 분석 지표 계산 (2일)

```
의존: [S4-T12]
파일:
  └── src/analytics/metrics.py (나머지 지표)

구현:
  • 지표 1~6: P&L, Brier, Edge실현율, 슬리피지, 쿨다운, ob_freeze
  • 지표 7: 시장 정합성 효과 (ALIGNED vs DIVERGENT)
  • 지표 8: 방향별 Edge 실현율 (Yes vs No)
  • 지표 9: Preliminary 정확도
  • 지표 10: Rapid Entry 가상 P&L
  • 지표 11: bet365 이탈 경고 효과
```

### [S7-T2] 적응적 파라미터 조정 (1일)

```
의존: [S7-T1]
파일:
  └── src/analytics/adaptive_params.py

구현:
  • adaptive_parameter_update(analytics) → 7개 파라미터 조정
  • 파라미터 변경 이력 DB 저장
```

### [S7-T3] DailyAnalytics CRON (1일)

```
의존: [S7-T1], [S7-T2]
파일:
  └── src/analytics/daily.py

구현:
  • 매일 자정: 정산 경기 분석 + 파라미터 조정 + 리포트 생성
```

### [S7-T4] Layer 3 Analytics 확장 (2일)

```
의존: [S7-T1], [S6-T6]
파일:
  ├── CalibrationPlot.jsx    # 3B
  ├── DirectionalAnalysis.jsx # 3D
  ├── Bet365Effect.jsx       # 3E
  ├── PrelimAccuracy.jsx     # 3F
  └── ParamHistory.jsx       # 3G
```

### Sprint 7 완료 기준

```
□ 11개 분석 지표 자동 계산
□ 일일 리포트 Slack 발송
□ 적응적 파라미터 조정 로직 작동
□ Layer 3 Analytics 전체 뷰 브라우저 표시
```

---

## Sprint 8: Phase 0 PAPER 트레이딩 (2~4주 운영)

> **목표:** 실제 경기에서 24/7 무인 PAPER 운영. Go/No-Go 데이터 축적.

### [S8-T1] PAPER 운영 시작 + 안정화 (1주)

```
수행:
  • trading_mode: "paper"로 24/7 운영 시작
  • 첫 주: 버그 발견 + 즉시 수정 사이클
  • 스케줄러 → 엔진 스폰 → Phase 2~4 → 정산 자동 확인
  • Slack 알림 정상 수신 확인

관찰:
  • 경기당 평균 시그널 수
  • PRELIMINARY → CONFIRMED 지연 시간
  • ob_freeze 발동 빈도
  • 엔진 크래시 빈도 (0이어야 함)
```

### [S8-T2] 데이터 축적 (2~3주)

```
수행:
  • 최소 50경기 PAPER 거래 축적 (100+ 거래)
  • 매일 DailyAnalytics 확인

축적 지표:
  • Brier Score 추이
  • Edge 실현율 (목표: 0.7~1.3)
  • 가상 P&L 추이 (양의 기울기)
  • Max Drawdown (목표: < 15%)
  • Preliminary 정확도 (목표: > 0.95)
  • 방향별 Edge 실현율 (Yes vs No 균형)
```

### [S8-T3] Phase A 전환 Go/No-Go 판정

```
Go 기준 (모두 충족):
  □ 누적 거래 ≥ 100
  □ Brier Score: Pinnacle 대비 개선
  □ Edge 실현율: 0.7~1.3
  □ 가상 P&L: 양수 (슬리피지 반영 후)
  □ Max Drawdown: < 15%
  □ Preliminary 정확도: > 0.90
  □ 엔진 크래시: 0회 (최근 2주)
  □ 데이터 소스 장애: 자동 복구 성공

No-Go 시:
  • 문제점 분석 → 코드 수정 → Sprint 8 연장
  • 모델 문제면 Phase 1 재학습 → Sprint 2 부분 재실행
```

---

## Sprint 9: Phase A LIVE 전환 (1주 준비 + 운영)

> **목표:** 실제 돈으로 보수적 트레이딩 시작.

### [S9-T1] LIVE 전환 준비 (2일)

```
수행:
  ├── config/system.live.yaml 최종 확인
  │   • trading_mode: "live"
  │   • K_frac: 0.25
  │   • z: 1.645
  │   • Kalshi API 키 설정
  │   • 초기 bankroll 설정
  │
  ├── Kalshi 계좌 확인
  │   • 잔고 확인
  │   • API 권한 확인
  │   • 주문 테스트 (1계약 매수/매도)
  │
  └── 모니터링 강화
      • Drawdown 알림 임계값: 10%
      • 일일 P&L 알림 활성화
      • Telegram Critical 알림 활성화
```

### [S9-T2] LIVE 운영 시작

```
수행:
  • trading_mode: "paper" → "live"
  • 첫 1주: 집중 모니터링 (대시보드 상시 확인)
  • 이상 발생 시 즉시 "paper"로 롤백

관찰:
  • 실제 체결가 vs Paper 예상 체결가 비교
  • 실제 슬리피지 측정
  • Kalshi API 응답 시간
  • 실제 P&L vs Paper P&L 비교
```

---

## 전체 타임라인 요약

```
Week  1-2:  Sprint 1 — 인프라 (+ 과거 데이터 수집 백그라운드)
Week  3-5:  Sprint 2 — Phase 1 (Offline Calibration)
Week  6-8:  Sprint 3 — Phase 2+3 코어 (μ/P_true 계산)
Week  9-11: Sprint 4 — 이벤트 처리 + Phase 4 실행
Week 12-13: Sprint 5 — 스케줄러 + 24/7 자동화
Week 13-15: Sprint 6 — 대시보드
Week 15-16: Sprint 7 — 사후 분석
Week 17-20: Sprint 8 — PAPER 운영 + 안정화
Week 21+:   Sprint 9 — LIVE 전환

총 예상: ~20주 (5개월)
```

---

## 태스크 의존성 전체 그래프

```
[S1-T1] Config
   ├──▶ [S1-T2] Logging
   ├──▶ [S1-T3] Redis ──▶ [S1-T4] PostgreSQL
   ├──▶ [S1-T5] Types
   └──▶ [S1-T6] Goalserve Client ──▶ [S1-T7] Data Collector
                                          │
   ┌──────────────────────────────────────┘
   ▼
[S2-T1] Intervals ──┬──▶ [S2-T2] Q Matrix ──────────┐
                    │                                 │
[S2-T3] Features ──▶ [S2-T4] XGBoost ───────────────┤
                                                      ▼
                                              [S2-T5] NLL ⭐
                                                      │
                                              [S2-T6] Validation
                                                      │
                                              [S2-T7] Recalibrate
                                                      │
   ┌──────────────────────────────────────────────────┘
   ▼
[S3-T1] Prematch Data ──▶ [S3-T2] a Parameter ──▶ [S3-T3] Sanity
                                                        │
[S3-T4] Model Init ◀───────────────────────────────────┘
   │
[S3-T5] MC Core ⭐ ──▶ [S3-T6] Remaining μ ──▶ [S3-T7] Pricing
   │                                                │
   └──────────────────────────────────────▶ [S3-T8] Replay Engine
                                                │
   ┌────────────────────────────────────────────┘
   ▼
[S4-T1] State Machine
   │
   ├──▶ [S4-T2] Live Odds WS ──┐
   ├──▶ [S4-T3] Live Score REST ┤
   │                             ▼
   ├──▶ [S4-T4] Event Handler ──▶ [S4-T5] ob_freeze
   │                             │
   │    [S4-T6] Stoppage ◀──────┘
   │
   ├──▶ [S4-T7] Kalshi Client ──▶ [S4-T8] Edge Detection (v2) ⭐
   │                                    │
   │                              [S4-T9] Kelly + Risk
   │                                    │
   │                              [S4-T10] Exit Logic (v2) ⭐
   │                                    │
   │                              [S4-T11] Execution (Paper v2)
   │                                    │
   │                              [S4-T12] Settlement (v2)
   │                                    │
   └────────────────────────────▶ [S4-T13] MatchEngine 통합
                                        │
   ┌────────────────────────────────────┘
   ▼
[S5-T1] Scheduler ──▶ [S5-T2] systemd ──▶ [S5-T3] Alerts
                                               │
[S6-T1] Dashboard Server ──▶ [S6-T2] React ──▶ [S6-T3] PriceChart ⭐
   │                                           │
   └──▶ [S6-T4] Layer 1 ──▶ [S6-T5] Layer 2 ──▶ [S6-T6] Layer 3
                                                       │
[S7-T1] Metrics ──▶ [S7-T2] Adaptive ──▶ [S7-T3] Daily CRON
                                               │
                                         [S7-T4] Analytics UI
                                               │
                                         [S8] PAPER 운영
                                               │
                                         [S9] LIVE 전환
```