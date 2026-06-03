# 토픽 분류 파이프라인

미배정 이벤트를 LLM과 임베딩 유사도 검색으로 적절한 토픽에 연결하는 배치 파이프라인.


## 컴포넌트 구조

```
classify_topics.py              # 실행 진입점
src/
  embedding.py                  # 임베딩 모델 싱글톤 (ko-sRoBERTa-multitask) (공유)
  openai_client/                # LLM 클라이언트 (공유)
  db/
    events.py                   # 이벤트 조회·갱신 repository (공유)
    topics.py                   # 토픽 생성 repository
    topic_causes.py             # 토픽 후보 검색·cause 적재 repository
  topic_classifier/
    settings.py                 # 배치 크기, 임계값 등 설정 상수
    prompts.py                  # LLM 프롬프트 빌더
    pipeline.py                 # 파이프라인 오케스트레이션
```

`src/db/`와 `src/embedding.py`, `src/openai_client/`는 이벤트 분류 등 다른 파이프라인과 공유한다.
토픽 분류 전용 코드는 `src/topic_classifier/`에 격리된다.


## 처리 흐름

배치 단위로 미배정 이벤트(`topic_id IS NULL`)를 순차 처리한다.

1. **이벤트 조회** — `article_count - abusing_count >= N`을 만족하는 이벤트를 `created_at` 오름차순으로 최대 `BATCH_SIZE`건 가져온다.

2. **원인·결과 추출** — LLM에 이벤트 제목·요약을 전달해 원인(cause)과 결과(result)를 자연어로 추출한다.

3. **토픽 후보 검색** — 원인을 임베딩해 `topic_causes` 테이블에서 코사인 유사도 상위 `TOP_K`개 토픽을 검색한다.

4. **토픽 배정 결정** — LLM이 이벤트와 후보 토픽을 비교해 기존 토픽 배정(`assign`) 또는 신규 토픽 생성(`create`)을 결정한다.

5. **DB 반영** — 단일 트랜잭션으로 처리한다.
   - 토픽 확정 (생성 또는 기존 id 사용)
   - `events.topic_id` 업데이트
   - 결과(result)를 임베딩해 `topic_causes`에 적재
   - 동일 토픽 내 이전 이벤트와 `prev_event_id`/`next_event_id` 양방향 연결


## 주요 설계 결정

| 결정 | 이유 |
|------|------|
| 이벤트를 `created_at` 순으로 **순차** 처리 | 후행 이벤트가 선행보다 먼저 배정되면 prev/next 체인이 깨진다 |
| 오류 발생 시 배치 **중단** (skip 없음) | 체인 순서를 보장하기 위해. 미처리 이벤트는 다음 실행에서 자동 재시도 |
| `topic_causes.cause_text`에 **결과(result)** 를 저장 | "이번 이벤트의 결과가 유사 사건의 원인으로 작용한다"는 모델링 가정 |
| 임베딩 모델을 **싱글톤**으로 로드 | 다른 파이프라인과 공유 시 중복 로드 방지 |


## 실행 방법

```bash
# 환경변수 설정 (.env 또는 직접)
DATABASE_URL=postgresql://...
OPENAI_API_KEY=sk-...

# 실행
python classify_topics.py
```

Supabase에 `events`, `topics`, `topic_causes` 테이블과 pgvector가 사전 적용되어 있어야 한다.
