"""토픽 분류 파이프라인 설정 상수.

MIN_NET_ARTICLE_COUNT, BATCH_SIZE, TOP_K는 임의 초기값이므로 운영 중 조정이 필요할 수 있다.
"""

# (article_count - abusing_count) 최소값.
# 실제 기사 수가 이 값 미만인 이벤트는 분류 대상에서 제외한다.
MIN_NET_ARTICLE_COUNT = 5

# 한 번의 실행에서 처리할 최대 이벤트 수.
# created_at ASC 순서가 보장되어야 체인이 올바르게 연결되므로 반드시 순차 처리한다.
BATCH_SIZE = 10

# 토픽 후보 검색 시 반환할 최대 토픽 수 (코사인 거리 오름차순).
TOP_K = 5

# LLM 모델. OpenAIClient 기본값과 일치시킨다.
LLM_MODEL = "gpt-4.1-mini"
