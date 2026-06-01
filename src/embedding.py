"""공유 임베딩 유틸리티.

ko-sRoBERTa-multitask 모델을 싱글톤으로 관리하고, 텍스트 임베딩과
pgvector 바인딩용 문자열 직렬화를 제공한다.

이벤트 분류와 토픽 분류 모두 동일한 모델을 사용하므로 이 모듈에서
1회만 로드해 불필요한 메모리·시간 낭비를 방지한다.
"""

import os

# 임베딩 백엔드로 PyTorch만 사용하도록 명시한다(TensorFlow 미사용).
os.environ.setdefault("USE_TF", "0")

EMBED_MODEL = "jhgan/ko-sroberta-multitask"

_model = None


def _get_model():
    global _model
    if _model is None:
        print("[embed] 모델 로드 중...", flush=True)
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(EMBED_MODEL)
        print("[embed] 모델 로드 완료", flush=True)
    return _model


def embed(text: str) -> list[float]:
    """텍스트를 ko-sRoBERTa 모델로 임베딩하여 float 리스트로 반환한다 (768차원)."""
    return _get_model().encode(text).tolist()


def to_vector_literal(vec: list[float]) -> str:
    """float 리스트를 pgvector SQL 바인딩용 문자열로 직렬화한다.

    SQL에서 ?::vector 캐스트와 함께 사용한다:
        conn.query(SQL, (to_vector_literal(embedding),))
    pgvector는 '[f1,f2,...]' 형식 문자열을 벡터로 파싱한다.
    """
    return "[" + ",".join(map(str, vec)) + "]"
