import os

from dotenv import load_dotenv


# classify_events.py가 pipeline을 import하는 순간 설정이 평가되므로 .env를 먼저 읽는다.
load_dotenv(override=True)

BATCH_SIZE = int(os.environ.get("EVENT_BATCH_SIZE", "5"))
CANDIDATE_POOL_SIZE = int(os.environ.get("EVENT_CANDIDATE_LIMIT", "12"))

# 결정점 0(2026-07-22 확정, .omc/plans/event-classification-ner-redesign-plan.md):
# DISTANCE_THRESHOLD 단일 상수 게이트를 제거하고, 벡터 측은 top-K_vec ANN(K_VEC)이,
# 엔티티 측은 아래 4중 하드 필터가 정밀도 바닥을 구조적으로 대신한다.
K_VEC = int(os.environ.get("EVENT_VECTOR_FETCH_LIMIT", "100"))
ENTITY_CANDIDATE_LIMIT = int(os.environ.get("EVENT_ENTITY_CANDIDATE_LIMIT", "50"))
ENTITY_DF_CUTOFF_EVENTS = int(os.environ.get("EVENT_ENTITY_DF_CUTOFF_EVENTS", "20"))
ENTITY_DF_CUTOFF_DAYS = int(os.environ.get("EVENT_ENTITY_DF_CUTOFF_DAYS", "90"))
ENTITY_DISTANCE_CEILING = float(os.environ.get("EVENT_ENTITY_DISTANCE_CEILING", "0.7"))
MIN_SHARED_ENTITIES = int(os.environ.get("EVENT_MIN_SHARED_ENTITIES", "2"))
# 배정 판단은 Jev 직접 배정(2026-09-24 결정안). 임계값은 낮추지 않는다(판정 완화 금지).
JEV_URL = os.environ.get("JEV_URL", "https://api.typesafe.ai/v1/systemone")
# 313건 검증은 jev-1.13.0으로 했다. jev-latest는 2026-10월 말 1.14로 넘어갔으므로 검증한 버전으로 고정한다.
JEV_MODEL = os.environ.get("JEV_MODEL", "jev-1.13.0")
JEV_ASSIGN_THRESHOLD = float(os.environ.get("EVENT_JEV_ASSIGN_THRESHOLD", "0.30"))
LLM_MODEL = os.environ.get(
    "LLM_EVENT_MODEL",
    os.environ.get(
        "LLM_TOPIC_EVENT_MODEL",
        os.environ.get("LLM_DEFAULT_MODEL", "qwen/qwen3-30b-a3b-instruct-2507"),
    ),
)
