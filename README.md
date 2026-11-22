# Hannoon AI

RSS 뉴스 기사를 수집하고, 원문을 크롤링한 뒤 LLM(OpenAI 호환 API, 기본 OpenRouter)과
로컬 임베딩으로 기사 분석, 이벤트 묶기, 토픽 묶기를 수행하는 파이프라인입니다.

현재 기본 전략은 비용과 품질을 나눠 쓰는 방식입니다.

- 기사 본문 정제, 요약, 어뷰징 판단: `solar-mini`
- 이벤트/토픽 배정처럼 장기 데이터 구조에 영향을 주는 판단: `solar-pro3`
- 이벤트 분류의 추출(대표 사건·부수 사건·고유명사)·이벤트명 생성: `qwen/qwen3-30b-a3b-instruct-2507`
- 이벤트 배정 판단: 판정 전용 모델 Jev(TypeSafe, `jev-1.13.0`)
- 후보 검색·저장용 임베딩: 로컬 `jhgan/ko-sroberta-multitask` (768차원, API 비용 없음)

## 전체 흐름

```text
RSS 수집
-> 기사 원문 크롤링
-> 필요한 경우에만 LLM 본문 정제
-> LLM 요약 + 어뷰징 판단
-> LLM으로 major(핵심 사건)/minor(부수 사건)/entities(고유명사) 추출
-> major 임베딩(pgvector) + 엔티티 역색인으로 기존 이벤트 후보 검색
-> Jev가 후보별 "같은 사건일 확률"을 내고 기존 이벤트 배정 또는 새 이벤트 생성
-> 이벤트 cause/result 추출
-> cause query 임베딩으로 기존 토픽 후보 검색
-> solar-pro3로 기존 토픽 배정 또는 새 토픽 생성
-> passage 임베딩 저장
```

후보 검색 결과가 하나도 없으면 배정 판단 호출을 생략하고 바로 새 이벤트/토픽을 만듭니다.

## 이벤트 분류

1. **추출**: 기사 제목+AI 요약에서 LLM이 `major`(핵심 사건 4~5문장), `minor`(부수 사건),
   `entities`(정식 명칭 고유명사)를 JSON으로 뽑습니다. `major`가 작성일 이후의 일을 완료형으로
   쓰면 해당 문장을 지적해 한 번 다시 요청합니다.
2. **후보 검색** (후보 이벤트 최대 `EVENT_CANDIDATE_LIMIT`=12)
   - 벡터: `major` 임베딩으로 기사 top-`EVENT_VECTOR_FETCH_LIMIT`(100) 검색
   - 엔티티: `article_entities` 역색인에서 흔한 엔티티를 빼고 2개 이상 공유하는 기사
   - 기사 멤버 단위 점수(0.7×거리 + 0.3×(1−엔티티 겹침))로 이벤트별 최선 멤버를 대표로 씁니다.
3. **배정 판단**: Jev가 후보마다 "새 기사와 같은 구체적 사건일 확률"을 계산합니다. 최댓값이
   `EVENT_JEV_ASSIGN_THRESHOLD`(0.30) 이상이면 그 이벤트에 배정하고, 아니면 새 이벤트를 만듭니다.
   새 이벤트 이름은 LLM이 명사형으로 만들어 고정합니다. Jev 호출이 끝내 실패하면 그 기사는
   다음 실행에서 다시 처리됩니다.

`CANDIDATE_LOG_PATH`를 설정하면 기사별 후보 목록·Jev 확률·판정을 JSONL로 남깁니다.

## 설치

```powershell
python -m pip install -r requirements.txt
copy .env.example .env
```

`.env`에 최소한 아래 값을 설정합니다.

```env
LLM_API_KEY=your_openrouter_api_key
LLM_BASE_URL=https://openrouter.ai/api/v1
TYPESAFE_API_KEY=your_typesafe_api_key
DATABASE_URL=your_postgres_database_url
```

임베딩은 로컬 모델이라 API 키가 필요 없습니다(첫 실행 때 모델을 내려받습니다).

## 권장 모델 설정

```env
LLM_DEFAULT_MODEL=solar-mini
LLM_CLEANUP_MODEL=solar-mini
LLM_ARTICLE_MODEL=solar-mini
LLM_ABUSE_MODEL=solar-mini
LLM_SUMMARY_MODEL=solar-mini
LLM_ABUSE_ENABLED=false

LLM_TOPIC_EVENT_MODEL=solar-pro3
LLM_TOPIC_MODEL=solar-pro3
LLM_EVENT_MODEL=qwen/qwen3-30b-a3b-instruct-2507

JEV_MODEL=jev-1.13.0
EVENT_JEV_ASSIGN_THRESHOLD=0.30

EMBEDDING_MODEL=jhgan/ko-sroberta-multitask
EMBEDDING_DIMENSIONS=768
```

`JEV_MODEL`은 검증한 버전(`jev-1.13.0`)으로 고정합니다. `jev-latest`는 새 버전으로 자동
전환되므로 임계값(0.30)이 그대로 맞는다는 보장이 없습니다.

본문 전체를 자주 넣는 작업은 `solar-mini`로 두고, 이벤트/토픽 배정처럼 잘못되면
DB 구조가 오래 꼬이는 판단만 `solar-pro3`를 사용합니다.

## 실행

전체 기사 수집/크롤링/분석:

```powershell
python main.py run
```

단계별 실행:

```powershell
python main.py fetch
python main.py crawl
python main.py process
```

이벤트 분류:

```powershell
python classify_events.py --database-url "postgresql://..."
```

토픽 분류:

```powershell
python classify_topics.py --database-url "postgresql://..."
```

주요 옵션:

- `--no-llm-cleanup`: 크롤링 본문 LLM 정제를 끕니다.
- `--llm-cleanup-model`: 본문 정제 모델을 지정합니다.
- `--llm-abuse-model`: 어뷰징 판단 모델을 지정합니다.
- `--llm-abuse` / `--no-llm-abuse`: 기사 분석 단계의 어뷰징 판단을 켜거나 끕니다.
- `--llm-summary-model`: 요약 모델을 지정합니다.
- `--ai-batch-size`: 한 번에 LLM 분석할 기사 수입니다.
- `--analysis-max-attempts`: 기사 분석 실패 확정 전 재시도 횟수입니다.
- `--batch-size`: 이벤트/토픽 분류 배치 크기입니다.
- `--top-k`: LLM에 보여줄 후보 수입니다.

## 데이터베이스 주의사항

로컬 SQLite는 RSS 수집, 크롤링, 기사 분석 개발용입니다. 이벤트/토픽 분류는
Supabase/Postgres와 pgvector가 필요합니다.

로컬 `ko-sroberta-multitask` 임베딩은 768차원입니다. 임베딩을 저장하는 pgvector 컬럼
(`articles.embedding`, `events.embedding`, `topic_causes.cause_embedding`)은 `vector(768)`이어야
하고, 이벤트 분류는 `articles.minor`(jsonb)와 `article_entities` 테이블을 씁니다.
스키마는 hannoon-supabase 마이그레이션으로 맞춥니다.

기존 Upstage 4096차원 임베딩과 768차원 임베딩은 서로 비교할 수 없습니다.
스키마를 바꾼 뒤에는 기존 저장 벡터를 다시 생성해야 합니다.

운영 DB에는 최소한 다음 테이블이 준비되어 있어야 합니다.

- `feeds`
- `articles`
- `article_jobs`
- `article_ai_results`
- `events`
- `topics`
- `topic_causes`
- `event_articles`
- `abusing_articles`
- `article_entities`

현재 애플리케이션은 운영 DB에서 테이블을 자동 생성하지 않습니다. Supabase
migration에서 스키마를 먼저 적용한 뒤 실행해야 합니다.

## 비용을 줄이는 동작

- 크롤링 본문 LLM 정제는 광고, 공유 UI, 저작권 문구 등 정제 흔적이 있을 때만 호출합니다.
- 이벤트/토픽 후보가 없으면 배정 판단을 생략하고 바로 새 항목을 만듭니다.
- 이벤트 배정 판단은 LLM이 아니라 Jev(입력 100만 토큰당 $0.042, 출력 무료)로 합니다.
- `LLM_ABUSE_ENABLED=false`이면 기사 분석 단계에서 어뷰징 판단을 건너뛰고
  `normal`로 저장해 요약 처리 성공률을 우선합니다.
- 어뷰징 판단을 켰고 어뷰징 판단 모델과 요약 모델이 같으면 한 번의 LLM 호출로
  요약과 어뷰징 판단을 함께 처리합니다.

## 주요 파일

- `main.py`: RSS 수집/크롤링/기사 분석 CLI 진입점
- `classify_events.py`: 기사에서 이벤트를 배정하거나 생성하는 CLI 진입점
- `classify_topics.py`: 이벤트를 토픽에 배정하거나 생성하는 CLI 진입점
- `src/collector/`: RSS 수집, 크롤링, 기사 LLM 분석
- `src/event_classifier/`: 이벤트 추출(`pipeline.py`, `prompts.py`)과 Jev 배정 판단(`jev.py`)
- `src/topic_classifier/`: 토픽 배정과 토픽 cause/result 누적
- `src/db/`: Postgres/Supabase repository 함수
- `src/event_summary.py`: 새 이벤트 이름 생성
- `src/diagnostics.py`: 후보 검색·판정 진단 로그
- `src/embedding.py`: 로컬 ko-sRoBERTa(sentence-transformers) 임베딩 래퍼
- `src/openai_client/`: OpenAI 호환 Chat Completions 클라이언트(기본 OpenRouter)
