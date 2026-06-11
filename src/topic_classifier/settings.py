import os

from dotenv import load_dotenv


# classify_topics.py가 CLI 모듈을 import할 때 환경 변수 기반 기본값이 확정된다.
load_dotenv(override=True)

MIN_NET_ARTICLE_COUNT = int(os.environ.get("TOPIC_MIN_NET_ARTICLE_COUNT", "5"))
BATCH_SIZE = int(os.environ.get("TOPIC_BATCH_SIZE", "10"))
TOP_K = int(os.environ.get("TOPIC_CANDIDATE_LIMIT", "12"))
LLM_MODEL = os.environ.get(
    "LLM_TOPIC_MODEL",
    os.environ.get("LLM_TOPIC_EVENT_MODEL", os.environ.get("LLM_DEFAULT_MODEL", "solar-mini")),
)
