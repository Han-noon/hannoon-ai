"""이벤트 분류 파이프라인 설정 상수."""

BATCH_SIZE = 5         # 한 번의 루프에서 처리할 기사 개수
TOP_K = 4              # 정밀 비교할 벡터 후보군 개수
DISTANCE_THRESHOLD = 0.2  # 벡터 최대 거리 (1 - 0.8 유사도)
FINAL_THRESHOLD = 0.8   # LLM + 벡터 융합 최종 통과 점수
LLM_MODEL = "gpt-4.1-mini"