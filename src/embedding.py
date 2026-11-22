"""Shared embedding utilities backed by a local ko-sRoBERTa sentence-transformer.

비용 절감을 위해 외부 임베딩 API 대신 로컬 `jhgan/ko-sroberta-multitask`(768차원)를
사용한다. ko-sRoBERTa는 대칭(STS) 모델이라 query/passage 구분이 없으므로 두 함수
모두 동일 모델로 임베딩한다. 호출부 호환을 위해 함수 시그니처는 그대로 유지한다.
"""

from __future__ import annotations

import os
import re
import sys

from dotenv import load_dotenv


load_dotenv(override=True)

# transformers가 (이 환경에 깨진) TensorFlow를 끌어오지 않게 한다. 우리는 torch만 쓴다.
os.environ.setdefault("USE_TF", "0")

DEFAULT_EMBEDDING_MODEL = "jhgan/ko-sroberta-multitask"
DEFAULT_EMBEDDING_DIMENSIONS = 768

_model = None  # lazy SentenceTransformer 싱글톤


def _get_model():
    """Load the sentence-transformer once per process."""
    global _model
    if _model is None:
        # 무거운 의존성(torch)이라 실제 임베딩 시점까지 import를 지연한다.
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(_load_model_name())
    return _model


def embed_query(text: str) -> list[float]:
    """Embed text used to search existing stored passages (대칭 모델: passage와 동일)."""
    return _embed(text)


def embed_passage(text: str) -> list[float]:
    """Embed text that will be stored as a searchable passage."""
    return _embed(text)


def _embed(text: str) -> list[float]:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        raise ValueError("Cannot embed empty text.")

    model = _get_model()
    # 모델 최대 길이(ko-sRoBERTa 128토큰)를 넘는 입력은 뒷부분이 조용히 잘리므로 경고만 남긴다.
    token_count = _token_count(model, cleaned)
    if token_count > model.max_seq_length:
        print(
            f"[embedding] input truncated: {token_count} > {model.max_seq_length} tokens",
            file=sys.stderr,
        )
    vector = model.encode(cleaned).tolist()
    _validate_dimensions(vector)
    return vector


def _token_count(model, text: str) -> int:
    """특수 토큰 포함 실제 토큰 수. 토크나이저 기본값이 최대 길이에서 잘라 세므로 truncation을 끈다."""
    return len(model.tokenizer(text, truncation=False)["input_ids"])


def fit_sentences_to_max_tokens(text: str) -> str:
    """모델 최대 길이 안에 드는 앞쪽 문장들만 남긴다(문장 중간에서 잘리지 않게).

    첫 문장은 항상 남긴다. 첫 문장만으로도 넘치면 그대로 두고 _embed가 경고한다.
    """
    model = _get_model()
    sentences = [s for s in re.split(r"(?<=[.!?。！？])\s+", " ".join((text or "").split())) if s]
    kept: list[str] = []
    for sentence in sentences:
        candidate = " ".join(kept + [sentence])
        if kept and _token_count(model, candidate) > model.max_seq_length:
            break
        kept.append(sentence)
    return " ".join(kept)


def _load_model_name() -> str:
    return os.getenv("EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL


def _load_dimensions() -> int:
    raw_value = os.getenv("EMBEDDING_DIMENSIONS") or str(DEFAULT_EMBEDDING_DIMENSIONS)
    try:
        dimensions = int(raw_value)
    except ValueError as exc:
        raise ValueError("EMBEDDING_DIMENSIONS must be an integer.") from exc
    if dimensions <= 0:
        raise ValueError("EMBEDDING_DIMENSIONS must be greater than 0.")
    return dimensions


def _validate_dimensions(vector: list[float]) -> None:
    expected_dimensions = _load_dimensions()
    if len(vector) != expected_dimensions:
        raise ValueError(
            f"Embedding model returned {len(vector)} dimensions, "
            f"but EMBEDDING_DIMENSIONS is {expected_dimensions}."
        )


def to_vector_literal(vec: list[float]) -> str:
    """float 리스트를 pgvector SQL 바인딩용 문자열로 직렬화한다.

    SQL에서 `?::vector` 캐스트와 함께 사용한다.
    pgvector는 `[f1,f2,...]` 형식 문자열을 벡터로 파싱한다.
    """
    return "[" + ",".join(map(str, vec)) + "]"
