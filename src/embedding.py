"""Shared embedding utilities backed by the Upstage Embedding API."""

from __future__ import annotations

import os

from dotenv import load_dotenv
from openai import OpenAI


load_dotenv(override=True)

DEFAULT_EMBEDDING_BASE_URL = "https://api.upstage.ai/v1"
DEFAULT_EMBEDDING_MODEL = "solar-embedding-1-large-passage"
DEFAULT_EMBEDDING_QUERY_MODEL = "solar-embedding-1-large-query"
DEFAULT_EMBEDDING_PASSAGE_MODEL = "solar-embedding-1-large-passage"
DEFAULT_EMBEDDING_DIMENSIONS = 4096

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    """Create the OpenAI-compatible client once per process."""
    global _client
    if _client is None:
        load_dotenv(override=True)
        api_key = (
            os.getenv("EMBEDDING_API_KEY")
            or os.getenv("UPSTAGE_API_KEY")
            or os.getenv("LLM_API_KEY")
            or os.getenv("OPENAI_API_KEY")
        )
        if not api_key:
            raise ValueError(
                "Set EMBEDDING_API_KEY or UPSTAGE_API_KEY before embedding text."
            )

        _client = OpenAI(
            api_key=api_key,
            base_url=_load_base_url(),
            timeout=_load_timeout_seconds(),
            max_retries=_load_max_retries(),
        )
    return _client


def embed(text: str, *, model: str | None = None) -> list[float]:
    """Embed text with Upstage and return a 4096-dimensional vector by default."""
    return _embed(text, model=model or _load_model())


def embed_query(text: str) -> list[float]:
    """Embed text used to search existing stored passages."""
    return _embed(text, model=_load_query_model())


def embed_passage(text: str) -> list[float]:
    """Embed text that will be stored as a searchable passage."""
    return _embed(text, model=_load_passage_model())


def _embed(text: str, *, model: str) -> list[float]:
    cleaned = " ".join((text or "").split())
    if not cleaned:
        raise ValueError("Cannot embed empty text.")

    response = _get_client().embeddings.create(input=cleaned, model=model)
    vector = response.data[0].embedding
    _validate_dimensions(vector, model)
    return vector


def _load_model() -> str:
    return os.getenv("EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL


def _load_query_model() -> str:
    return os.getenv("EMBEDDING_QUERY_MODEL") or DEFAULT_EMBEDDING_QUERY_MODEL


def _load_passage_model() -> str:
    return (
        os.getenv("EMBEDDING_PASSAGE_MODEL")
        or os.getenv("EMBEDDING_MODEL")
        or DEFAULT_EMBEDDING_PASSAGE_MODEL
    )


def _load_dimensions() -> int:
    raw_value = os.getenv("EMBEDDING_DIMENSIONS") or str(DEFAULT_EMBEDDING_DIMENSIONS)
    try:
        dimensions = int(raw_value)
    except ValueError as exc:
        raise ValueError("EMBEDDING_DIMENSIONS must be an integer.") from exc
    if dimensions <= 0:
        raise ValueError("EMBEDDING_DIMENSIONS must be greater than 0.")
    return dimensions


def _load_base_url() -> str:
    return (
        os.getenv("EMBEDDING_BASE_URL")
        or os.getenv("UPSTAGE_BASE_URL")
        or os.getenv("LLM_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or DEFAULT_EMBEDDING_BASE_URL
    )


def _load_timeout_seconds() -> float:
    raw_value = (
        os.getenv("EMBEDDING_TIMEOUT_SECONDS")
        or os.getenv("LLM_TIMEOUT_SECONDS")
        or os.getenv("OPENAI_TIMEOUT_SECONDS")
        or "60"
    )
    try:
        timeout = float(raw_value)
    except ValueError as exc:
        raise ValueError("EMBEDDING_TIMEOUT_SECONDS must be a number.") from exc
    if timeout <= 0:
        raise ValueError("EMBEDDING_TIMEOUT_SECONDS must be greater than 0.")
    return timeout


def _load_max_retries() -> int:
    raw_value = (
        os.getenv("EMBEDDING_MAX_RETRIES")
        or os.getenv("LLM_MAX_RETRIES")
        or os.getenv("OPENAI_MAX_RETRIES")
        or "0"
    )
    try:
        max_retries = int(raw_value)
    except ValueError as exc:
        raise ValueError("EMBEDDING_MAX_RETRIES must be an integer.") from exc
    if max_retries < 0:
        raise ValueError("EMBEDDING_MAX_RETRIES must be greater than or equal to 0.")
    return max_retries


def _validate_dimensions(vector: list[float], model_name: str) -> None:
    expected_dimensions = _load_dimensions()
    if len(vector) != expected_dimensions:
        raise ValueError(
            f"{model_name} returned {len(vector)} dimensions, "
            f"but EMBEDDING_DIMENSIONS is {expected_dimensions}."
        )


def to_vector_literal(vec: list[float]) -> str:
    """float 리스트를 pgvector SQL 바인딩용 문자열로 직렬화한다.

    SQL에서 `?::vector` 캐스트와 함께 사용한다.
    pgvector는 `[f1,f2,...]` 형식 문자열을 벡터로 파싱한다.
    """
    return "[" + ",".join(map(str, vec)) + "]"
