# overseas-tax-agent

증권사 상담원용 **해외주식 양도소득세 민원 대응 에이전트**.
여러 증권사(계좌) 합산, 증권사별 선입선출/이동평균 차이, 대체입고(이체) 취득가액, 결제일 환율처럼 증권사 앱과 신고대행이 답하지 못하는 민원을 다룬다.

- ReAct 에이전트(직접 구현) · Text-to-SQL · 결정론적 세액 계산 · 폐쇄망 RAG
- FastAPI · PostgreSQL/pgvector → Oracle 23ai · Ollama · K8s(dmz/internal)

## 원칙

1. **숫자는 코드가 계산한다.** 세액은 `app/tools/tax_calc.py`가 결정론적으로 계산하고 LLM은 설명만 한다. 초안의 숫자는 코드가 다시 검증한다.
2. **실데이터는 로컬에만.** 내 거래(`data/mine/`)는 git에도 퍼블릭 API에도 가지 않는다. 퍼블릭 API에는 합성 데이터만.
3. **폐쇄망에서 외부 폴백 금지.** 로컬 모델이 실패하면 실패로 끝낸다.

## 계산 단위

계산은 **계좌** 단위다 (`(account_id, ticker)` lot 큐). 각 계좌 결과가 그 증권사의 계산내역서 한 장에 해당하고, 납세자는 계좌 결과를 더해 250만 원 공제 후 22%를 낸다.
증권사마다 방식이 다르다(미래에셋·키움·KB·카카오페이·신한 = FIFO, 삼성·한투·대신·토스 = 이동평균, NH = 선택). 계좌 사이에 이체된 주식은 `TRANSFER_OUT/IN` 한 쌍으로 원 취득가액을 들고 간다(소득세법 §118의4).

> 처음에는 납세자 단위 단일 큐로 설계했다가, 증권사별 방식 차이와 계좌 단위 산정을 확인하고 전체를 다시 썼다. 근거: 국세청 서면-2022-국제세원-0764, 미래에셋 2025 신고대행 안내.

## 상태

| 모듈 | 상태 |
| --- | --- |
| `app/tools/tax_calc.py` | 완성. 계좌 단위 FIFO/AVG, TRANSFER 원가 승계, 결제일 환율, 통산·공제·22%. 테스트 15건 |
| `app/importers/` | 미래에셋·나무 CSV 매퍼, T+2 결제일 보정, 환율 부착, 중복 제거. 실제 헤더 확인 전 |
| `app/agent/react.py` | ReAct 루프. MAX_STEPS 8, E-LOOP, E-TOOL-FAIL, E-TOOL-ORDER, E-LLM-FORMAT |
| `app/llm/`, `app/judge/` | `LLMClient`·`Judge` Protocol. Claude/Ollama, Rule/Claude 구현체. 환경변수로 교체 |
| `app/tools/registry.py` | tax_calc 연결(요약 우선 응답). sql_query → Text-to-SQL, reg_search → 하이브리드 검색 |
| `app/tools/sql_guard.py`, `sql_exec.py`, `sql_query.py` | F04 Text-to-SQL. 아래 "거래 조회 보안" 절 |
| `app/repo.py` | 증권사별 방식 테이블, 계좌별 방식 결정 |
| `app/main.py` | `POST /v1/inquiries`, `POST /v1/tools/tax_calc`, `/healthz` |

테스트 24건 통과.

## 시작

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
make test
```

## 거래 조회 보안 (F04 Text-to-SQL, SCRUM-26·27)

에이전트가 자연어 의도로 `sql_query`를 부르면 LLM이 SQL을 제안하고, 세 겹을 통과해야 실행된다.

| 겹 | 모듈 | 하는 일 | 실패 코드 |
| --- | --- | --- | --- |
| 1 검사 | `sql_guard.validate` (sqlglot) | SELECT 한 문장만 · 허용 테이블 5개(trade/account/customer/fx_rate/settlement_daily) · DML/DDL/다중문/위험 함수 거부 · `customer_id = 숫자` 리터럴 거부 · LIMIT 500 강제 | `E-SQL-SCOPE` + 사유(MULTI_STATEMENT/PARSE/NOT_SELECT/WRITE_OR_DDL/FORBIDDEN_FUNC/TABLE_NOT_ALLOWED/NO_CUSTOMER_TABLE/CUSTOMER_LITERAL) |
| 2 바인드 | `sql_guard.bind_customer` | 고객 열이 있는 모든 테이블을 `(SELECT * FROM t WHERE customer_id = :customer_id)` 로 감싼다. LLM 이 어떤 고객을 적든 세션 고객만 조회 | — |
| 3 실행 | `sql_exec` | SQLite `mode=ro` + `PRAGMA query_only` (파서를 우회해도 DB가 거부) · 5초 초과 중단 · 500행 상한. PostgreSQL 은 읽기 전용 계정 `tax_ro`(`db/readonly_user.sql`, statement_timeout 5s), Oracle 은 `TAX_RO` + 동의어 + 프로파일(`db/trade_schema_oracle.sql`, `call_timeout`) | `E-SQL-EXEC` |

**방언 (F04-6, SCRUM-28)**: `TRADE_DB_KIND=sqlite|pg|oracle` 이 sqlglot 방언을 정한다. 파싱·출력·행 상한(LIMIT ↔ `FETCH FIRST n ROWS ONLY`)·별칭(`AS` 생략)은 sqlglot 이 바꾸고, 의미가 다른 날짜 리터럴(`DATE '2025-01-01'`, `TO_CHAR(settle_date,'YYYY')`)만 Oracle 전용 few-shot·스키마 설명으로 따로 둔다. 템플릿은 postgres 로 쓰고 실행 방언으로 transpile. Oracle 패키지 함수(`DBMS_LOCK.SLEEP`, `UTL_HTTP` …)는 FORBIDDEN_FUNC. 테스트 15건 `tests/test_sql_oracle.py` — 실 컨테이너 검증은 SCRUM-48.

거부되면 사유를 붙여 1회 재생성(F04-7), 그래도 거부면 `E-SQL-GEN`. 생성 경로는 `SQL_GEN=auto`: 표준 의도(전 거래 조회·종목 필터 — tax_calc 입력)는 템플릿(0ms·결정론), 집계·기간 조건이 섞인 의도만 LLM(Haiku, `SQL_MODEL`). 10/27 측정: Haiku 생성 3.6초가 30초 예산을 넘기는 결정타였고 에이전트 의도의 대부분이 표준형이었다(`scripts/show_latency.py`). 평가 구성(stub LLM)과 테스트는 `TEMPLATES`의 결정론 SQL을 쓴다. 테스트 19건 `tests/test_sql_guard.py` — 악의 8종 사유 코드 구분, 다른 고객 시도 → 세션 고객만, INSERT는 DB 단계 거부, 느린 쿼리 → 시간 초과.

```bash
python scripts/load_trade_db.py --src data/mine/trades.json   # 선택: 파일 DB(읽기 전용). 없으면 TRADES_FILE 로 메모리 DB
```

## 폐쇄망 구성 B (SCRUM-49)

환경변수만 바꿔 같은 코드가 외부 API 없이 돈다: `LLM_PROVIDER=ollama JUDGE_IMPL=local EMBEDDER=ollama`.

| 자리 | 구성 A (개발·상한선) | 구성 B (폐쇄망) |
| --- | --- | --- |
| 에이전트 | Claude Opus (`ClaudeClient`) | Ollama (`OllamaClient`) — 툴 호출 형식을 어기면 본문 JSON 에서 복원, `num_ctx` 16k |
| Judge | Claude Haiku (`ClaudeJudge`, tool_use 강제) | `LocalJudge` — 같은 유형 정의, Ollama JSON 스키마 출력. confidence 는 `LOCAL_JUDGE_SAMPLES` 반복 샘플링 일치 비율로 근사 가능 |
| 임베딩 | Ollama bge-m3 | 동일 |
| 실패 | — | 폴백 없음: Ollama 다운 → `FAILED/E-LLM`, Judge 실패 → `E-JUDGE` (`tests/test_no_fallback.py`) |

모델 비교: `python scripts/compare_local_models.py` (후보 × `eval/local_smoke.txt` 13건, 표 출력). K8s 배치는 `k8s/ollama.yaml` (internal 네임스페이스, 모델 볼륨).

### 모델 비교표 (SCRUM-49, 10/29, 맥 M2 Pro 32GB·Metal, `eval/local_smoke.txt` 13건, 타임아웃 120초)

| 모델 | final 도달 | 분류 정확 (REG 제외) | 숫자 정확 | 평균 스텝 | 평균/최대 시간 | 비고 |
| --- | --- | --- | --- | --- | --- | --- |
| **qwen2.5:7b** (선택) | 13/13 | 11/11 | 10/10 | 3.8 | 39.4s / 53.7s | 툴 호출 네이티브. 출력 짧음(건당 300토큰) |
| llama3.1:8b | 13/13 | 9/11 | 10/10 | 3.7 | 36.8s / 47.0s | 가장 빠르지만 분류 2건 틀림(TAX→MULTI, FX→TAX) |
| exaone3.5:7.8b | 12/13 | 11/11 | 10/10 | 2.7 | 67.2s / 89.4s | 툴 호출 미지원 → 텍스트 모드. 한국어 분류 좋음, 출력이 길어(1,000+토큰) 느림. E-TOOL-FAIL 1, 내부 용어 누출 1 |

읽기: ① 숫자는 세 모델 다 100% — 세금 계산이 모델 밖(코드)에 있다는 설계의 증거. ② 30초 타임아웃에서는 세 모델 다 최종 메모 전에 끊겼다(1차: qwen 3/13, llama 6/13). 로컬 7B 가 8~10k 토큰 문맥을 스텝마다 읽는 비용이 스텝당 8~10초. 맥 CPU/Metal 수치이고 GPU 수치는 SCRUM-45 에서 따로. ③ 선택은 qwen2.5:7b — 분류·완주·시간의 균형. exaone 은 분류기(LocalJudge)로는 동급이라 "분류는 exaone, 에이전트는 qwen" 조합도 가능.

## 데이터 경계 (NF, SCRUM-51)

실제 거래 데이터(`data/mine/`)는 어떤 경로로도 Anthropic API 로 나가지 않는다. 말이 아니라 `pytest` 가 매번 증명한다 (`tests/test_data_boundary.py`, 18건).

| 겹 | 모듈 | 하는 일 |
| --- | --- | --- |
| 1 구성 거부 | `egress.assert_synthetic_data` | `TRADES_FILE` 이 `data/mine/` 아래면 Claude 클라이언트(에이전트·Judge·재작성) 생성 자체가 `E-DATA-BOUNDARY` |
| 2 송신 검사 | `egress.check_outbound` | 나가는 payload(system·messages·tools)를 보내기 전에 훑는다. 이 기계에 `data/mine/trades.json` 이 있으면 거기서 뽑은 계좌·거래 ID 와 (종목, 수량, 단가) 조합을 지문으로, 없어도 주민번호·실계좌번호 꼴은 막는다. 걸리면 전송하지 않고 사유만 남긴다(값은 안 남김) |
| 3 단일 출구 | `egress.async_anthropic / anthropic` | `app/` 안에서 anthropic 클라이언트는 이 모듈로만 만든다. 테스트가 소스를 훑어 `Anthropic(` 직접 호출이 생기면 실패 |

지문은 메모리에만 있고 파일·로그·응답에 쓰지 않는다. 테스트는 진짜 `data/mine` 을 읽지 않고 `tmp_path` 의 가짜 실데이터(`MINE_TRADES_FILE`)로 돈다.

**폴백 금지** (`tests/test_no_fallback.py`): `LLM_PROVIDER=ollama` 에서 Ollama 가 죽으면 `FAILED/E-LLM` 으로 끝낸다 — 숫자·상태 문장은 코드로 나가고, Claude 로 넘어가지 않는다(anthropic 모듈을 '부르면 터지는' 가짜로 바꿔 두고 확인). CI(`.github/workflows/ci.yml`)는 경계·폴백 테스트를 별도 잡으로 먼저 돌리고, 키 없이 통과해야 한다.

## 내 데이터로 검증

```bash
# 1. 미래에셋·나무 앱에서 해외주식 거래내역 CSV 내보내기 → data/mine/
python scripts/import_my_trades.py
# 2. 증권사 계산내역서의 양도소득금액과 대조
python scripts/my_tax.py --year 2024 --expect MIRAE-001=<금액> --expect NAMUH-001=<금액> [--method NAMUH-001=AVG] [--lots]
```

## 평가 (F10) — 1차 결과, 10/24

같은 평가셋 200건(세액 120·규정 60·범위 밖 20, `eval/cases.jsonl`)을 네 구성에 돌렸다. Judge는 모두 `claude-haiku-5-5`, 에이전트 모델만 다르다. **답변 조립(F07)·RAG(F06)·임계값 보정 전의 기준점.**

| 지표 | stub (규칙+대본) | Haiku 5.5 | Sonnet 5.5 | Opus 5.5 |
| --- | --- | --- | --- | --- |
| 숫자 정확도 (코드 계산) | 100% | 99%¹ | 100% | 100% |
| 초안에 숫자 그대로 | 100% | 96% | 100% | 100% |
| 분류 정확도 | 66% | 100% | 100% | 100% |
| 범위 밖 차단 | 20% | 100% | 100% | 100% |
| 조문 적중 (RAG 전) | 5% | 27% | 30% | 25% |
| 표현 민감도 (3건 동일) | 55% | 95% | 85% | 95% |
| 중단 | 0 | 2 | 8 (E-LOOP 7) | 1 |
| 지연 p50 / p95 | — | 13 / 19s | 15 / 22s | 20 / 29s |
| 비용 (200건) | $0 | $0.7 | $6.2 | $12.6 |
| 재현성 (2회 결정 불일치) | 0 | — | — | 7/200 |

¹ 모델이 툴을 인자 없이 호출해 서버가 예외로 죽은 코드 결함 → 수정(E-TOOL-ARGS).

**LLM-as-judge 문장 품질** (SCRUM-44, 채점 모델 Sonnet 5.5, 각 1~5점, 숫자는 채점 대상 아님) — 최종 답에 도달한 건만:

| 모델 | n | ① 설명이 근거와 맞는가 | ② 상담원이 그대로 읽어 줄 수 있는가 | ③ 금지 표현 없음 |
| --- | --- | --- | --- | --- |
| Haiku 5.5 | 139 | 3.20 | 1.01 | 2.68 |
| Sonnet 5.5 | 134 | 3.49 | 1.09 | 3.12 |
| Opus 5.5 | 139 | 3.53 | 1.06 | 2.67 |

②가 1점대인 이유는 세 모델 모두 같다 — 현재 에이전트의 최종 답은 JSON(중괄호·영문 키·거래 ID)이고, "고객에게 안내해 주세요" 같은 상담원용 지시문과 "원문 확인 필요" 같은 내부 메모가 섞여 있다. **F07 조립기(고객용 문장과 내부 메모 분리) 전의 기준점**이며, 조립기 후 같은 채점을 다시 돌려 before/after를 본다. ③의 감점 사유도 대부분 내부 메모 노출 → 사후 검증(SCRUM-36) 금지 목록에 "내부 지시문" 추가.

**답변 조립(F07) before/after** (10/25, 같은 20건, 에이전트 Opus, 채점 Sonnet). "검증 통과"는 사후 검증기(`python -m app.answer.verify`)를 각 결과 파일에 똑같이 적용한 값.

| (같은 20건) | ① 근거 | ② 가독성 | ③ 금지 표현 | 검증 통과 | 주된 실패 |
| --- | --- | --- | --- | --- | --- |
| JSON 모드 (기준) | 3.42 | 1.16 | 2.79 | 0% | 내부 흔적·길이·계좌 ID |
| 프롬프트만 (`--final-format prose`) | 3.11 | **3.44** | 4.22 | **0/18** | 검색 안 한 조문 18/18 |
| 조립기 v1 | **3.58** | 2.63 | 4.42 | 14/19 | 600자 초과 5 |
| 조립기 v2 | 3.39 | 3.06 | **4.61** | **18/18** | — |

프롬프트만 바꾸면 가장 읽기 좋지만 **18건 전부 reg_search로 받지 않은 조문을 인용**(기억으로 적음)해서 실제로는 한 건도 내보낼 수 없다. 조립기는 문장 순서는 골격으로, 숫자·상태 문장은 코드로 넣고, 인용은 검색 결과 집합에서만 허용한다 → 검증 통과 0 → 18/18. 문장 품질은 프롬프트, **상태·인용·숫자는 코드**가 보장한다 (SCRUM-35·36). v1→v2는 채점 근거에서 원인을 찾아 고쳤다(문장 중간 절단, 600자 초과, 금액 중복, 낭독용 문장 속 조문 표기). v3(문장 수 축소, 방식 미확정 시 숫자가 가정임을 명시)는 실행 예정. 근거 점수 3.58↔3.39는 18건 표본의 노이즈 범위이고, 감점 사유 대부분이 코퍼스 부족(조문 7개) → RAG(F06) 몫.

읽는 법: 분류·차단은 Judge만으로 100%가 됐다. 숫자는 어느 모델도 틀리지 않지만 작은 모델은 문장에 옮겨 적을 때 빠뜨린다(→ F07이 숫자를 코드로 끼우는 이유). confidence 0.6 이상에서 분류 오답 0 → AUTO 임계값 조정(아래).

**규정 검색 평가 (SCRUM-32~34, 10/27)** — 코퍼스 159조각(법령·시행령 125 = 법제처 Open API 원문, 국세청 책자 32, 증권사 안내 2), 임베딩 bge-m3(로컬 Ollama, 100조각당 6.3초), 규정 질문 55문항, k=3 (`python -m eval.retrieval --judge`):

| 방식 | hit@3 (정답 조문) | MRR | answer@3 (직접 답함, LLM 판정) | 관련@3 |
| --- | --- | --- | --- | --- |
| 키워드 (BM25 2-gram) | 36% | 0.29 | 31% | 55% |
| 벡터 (bge-m3) | 42% | 0.38 | 35% | 67% |
| 하이브리드 (RRF) | 38% | 0.31 | 35% | 75% |
| **하이브리드 + 사전 확장** (기본) | **51%** | **0.41** | 36% | **78%** |

사전 확장(구어→법령 용어, "환율"→"외화 환산 기준환율 수령하거나 지출한 날")은 정답 조문 적중을 13%p 올린다 — 법령은 "결제일"·"대체입고"라는 말을 쓰지 않기 때문. 그러나 **질문에 직접 답하는 조각은 36%**, 상위 20개까지 봐도 44% → 순위가 아니라 **코퍼스 내용의 한계**다. 법은 원칙만 말하고("취득가액 = 실지거래가액") 고객은 구체적으로 묻는다("SEC fee도 필요경비예요?"). 재정렬로 얻을 수 있는 건 최대 +9%p라 질문-답 형식 자료 보강(SCRUM-53)이 먼저다. 소제목 단위 청킹(36→33%)과 코사인 기반 "약함" 판정(적중 0.64 vs 실패 0.67, 구분 불가)은 효과가 없어 쓰지 않는다. 검색이 약해도 답이 틀리지는 않는다 — 숫자·상태는 코드가 넣고 검색 안 된 조문은 검증기가 막는다. 설명의 구체성이 여기서 제한된다.

**게이팅 임계값 보정 (SCRUM-22, 10/24)** — Judge(Haiku)만 200문장 × 5회 = 1,000판정 (`python -m eval.judge_sweep`):

| AUTO 임계값 | AUTO 비율 | AUTO 중 분류 오답 |
| --- | --- | --- |
| 0.70 | 95% | 0 |
| 0.80 | 94% | 0 |
| **0.85** | **91%** | **0** |
| 0.90 | 57% | 0 |
| 0.95 (초기값) | 8% | 0 |

같은 문장 5회 confidence 표준편차 0.012, 분류가 바뀐 문장 0. 초기값 0.95는 사실상 전부 사람 검토로 보내는 설정이었다. **0.85로 변경** — 0.80과 오답률은 같지만(0) 확신도 분포(0.8대·0.9대)의 중간에 있어 경계 사례가 적고, 합성 데이터 기준이라 한 단계 보수적으로. 실데이터에서 AUTO 오답률 5% 이하가 확인되면 0.80 검토. REVIEW 하한 0.70은 유지(그 아래 5%는 모호한 문장). 전체 표·발견·한계는 노션 "평가 결과 1차". 재실행: `python -m eval.run --config A --repeat 2` → `python -m eval.report …`.

## 문서와 계획

- 기획서 · 기능 명세서: Claude Docs
- 작업 추적: Jira `SCRUM` 보드 — 에픽은 명세서 F번호, 커밋 메시지는 이슈 키로 시작
- 설계 결정 · 개념 정리: Notion 포트폴리오 페이지

## 로드맵

- S1 (10/21–10/25): ClaudeJudge, Claude 연결, Text-to-SQL 가드, 합성 데이터 300명, RAG 코퍼스·검색, 답변 조립·검증, 평가셋 200건과 구성 A 수치
- S2 (10/26–11/08): Oracle 전환, Ollama 로컬 구성, K8s NetworkPolicy, Spring 게이트웨이, 관측, 구성 B 평가와 A/B 비교표
