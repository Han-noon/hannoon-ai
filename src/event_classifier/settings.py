import os

from dotenv import load_dotenv


# classify_events.py가 pipeline을 import하는 순간 설정이 평가되므로 .env를 먼저 읽는다.
load_dotenv(override=True)

BATCH_SIZE = int(os.environ.get("EVENT_BATCH_SIZE", "5"))
TOP_K = int(os.environ.get("EVENT_CANDIDATE_LIMIT", "12"))
DISTANCE_THRESHOLD = float(os.environ.get("EVENT_DISTANCE_THRESHOLD", "0.45"))
ASSIGN_SCORE_THRESHOLD = float(os.environ.get("EVENT_ASSIGN_SCORE_THRESHOLD", "0.75"))
LLM_MODEL = os.environ.get(
    "LLM_EVENT_MODEL",
    os.environ.get("LLM_TOPIC_EVENT_MODEL", os.environ.get("LLM_DEFAULT_MODEL", "solar-mini")),
)
