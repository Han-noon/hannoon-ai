from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from typing import Any, Callable

import openai
from dotenv import load_dotenv
from openai import OpenAI


# LLM 응답 디스크 캐시 적중 통계 (프로세스 단위).
_CACHE_STATS = {"hits": 0, "misses": 0}

# 일시적 오류(요청 한도 초과·서버 오류·연결 실패)는 기사를 실패시키지 않고 그 자리에서 기다렸다 재시도한다.
# 실패시키면 러너가 그 기사를 맨 뒤로 미뤄 발행일 순서가 바뀌기 때문이다(2026-09-24 v14 rep1: 429로 41건).
_TRANSIENT_ERRORS = (
    openai.RateLimitError,
    openai.InternalServerError,
    openai.APIConnectionError,
    openai.APITimeoutError,
)
_BACKOFF_CAP_SECONDS = 120.0


class _RateLimiter:
    """프로세스 전체 LLM 호출 간격을 60/rpm초 이상으로 벌린다(캐시 적중은 제외)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self, rpm: float) -> None:
        if rpm <= 0:
            return
        with self._lock:
            now = time.monotonic()
            if self._next_at > now:
                time.sleep(self._next_at - now)
                now = self._next_at
            self._next_at = now + 60.0 / rpm


_RATE_LIMITER = _RateLimiter()


DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"
DEFAULT_LLM_MODEL = "gpt-4o-mini"

# OpenRouter 마이그레이션(2026-09-17): 기본 대상을 OpenRouter로 전환.
# 위 OpenAI 상수는 롤백/직접 OpenAI 사용 대비 그대로 둔다.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_DEFAULT_MODEL = "qwen/qwen3-30b-a3b-instruct-2507"
# streamlake가 qwen3-30b-a3b-instruct-2507 제공을 중단(2026-11-23 확인, 404)해 nebius로 고정한다.
OPENROUTER_PROVIDER_ONLY = ["nebius"]
OPENROUTER_ALLOW_FALLBACKS = False


class LLMClient:
    """OpenAI Chat Completions 클라이언트.

    기본 대상은 OpenAI(ChatGPT)다. 환경 변수 이름은 범용으로 유지해
    다른 OpenAI 호환 엔드포인트로 바꿔도 호출부를 고치지 않게 한다.
    """

    def __init__(
        self,
        model: str = OPENROUTER_DEFAULT_MODEL,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        provider_only: list[str] | None = None,
        allow_fallbacks: bool | None = None,
    ) -> None:
        load_dotenv(override=True)
        self.model = model
        self.timeout = timeout if timeout is not None else _load_timeout_seconds()
        self.max_rpm = _load_max_rpm()
        self.transient_retries = _load_transient_retries()

        resolved_api_key = (
            api_key
            or os.getenv("LLM_API_KEY")
            or os.getenv("OPENROUTER_API_KEY")
            or os.getenv("OPENAI_API_KEY")
        )
        if not resolved_api_key:
            raise ValueError(
                "Set LLM_API_KEY, OPENROUTER_API_KEY, or OPENAI_API_KEY before calling the LLM pipeline."
            )

        # 기본 대상은 OpenRouter. LLM_BASE_URL을 주면 그 값을 쓴다(다른 OpenAI 호환 API 사용 대비).
        resolved_base_url = base_url or os.getenv("LLM_BASE_URL") or OPENROUTER_BASE_URL

        self._client = OpenAI(
            api_key=resolved_api_key,
            base_url=resolved_base_url,
            timeout=self.timeout,
            # 재시도는 _create_with_retry 한 곳에서만 한다. SDK 재시도(구 LLM_MAX_RETRIES)를 켜 두면
            # 두 재시도가 곱해지고, SDK 쪽 재시도는 호출 간격 제한(LLM_MAX_RPM)을 거치지 않는다.
            max_retries=0,
        )

        # provider 지정(extra_body)은 OpenRouter 전용 필드라 다른 API로 보낼 때는 붙이지 않는다.
        self.provider_only = (
            provider_only
            if provider_only is not None
            else (_load_provider_only() if resolved_base_url.startswith(OPENROUTER_BASE_URL) else [])
        )
        self.allow_fallbacks = (
            allow_fallbacks if allow_fallbacks is not None else _load_allow_fallbacks()
        )

    def request(
        self,
        prompt: str,
        *,
        cache_validator: Callable[[str], bool] | None = None,
        **kwargs: Any,
    ) -> str:
        """응답 본문을 돌려준다.

        캐시(LLM_CACHE_DIR)는 쓸 수 있는 응답만 저장한다: response_format이 json_object면 JSON 객체로
        파싱돼야 하고, cache_validator가 있으면 그것도 통과해야 한다. 이미 저장된 응답이 이 검사에
        실패하면(과거에 저장된 깨진 응답) 버리고 다시 호출한다.
        """
        extra_body = kwargs.pop("extra_body", None) or {}
        if self.provider_only and "provider" not in extra_body:
            extra_body = {
                **extra_body,
                "provider": {"only": self.provider_only, "allow_fallbacks": self.allow_fallbacks},
            }
        if extra_body:
            kwargs["extra_body"] = extra_body

        expects_json = (kwargs.get("response_format") or {}).get("type") == "json_object"

        def usable(content: str) -> bool:
            try:
                if expects_json:
                    parse_json_object(content)
                return cache_validator is None or bool(cache_validator(content))
            except Exception:
                return False

        cache_path = self._cache_path(prompt, kwargs)
        if cache_path is not None and os.path.exists(cache_path):
            try:
                with open(cache_path, encoding="utf-8") as fp:
                    cached = json.load(fp)
            except (OSError, ValueError):  # 쓰다 끊긴 파일 등 읽을 수 없는 캐시도 버린다
                cached = {}
            if usable(cached.get("content") or ""):
                _CACHE_STATS["hits"] += 1
                print(
                    f"[llm-cache] hit (hits={_CACHE_STATS['hits']}, misses={_CACHE_STATS['misses']})",
                    file=sys.stderr,
                )
                return cached["content"]
            os.remove(cache_path)
            print("[llm-cache] discarded unusable cached response, calling again", file=sys.stderr)

        content = self._create_with_retry(prompt, kwargs)

        if cache_path is not None:
            _CACHE_STATS["misses"] += 1
        if cache_path is not None and not usable(content):
            print("[llm-cache] response not cached (unusable)", file=sys.stderr)
        elif cache_path is not None:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            tmp_path = f"{cache_path}.{os.getpid()}.tmp"  # 쓰는 도중 중단돼도 잘린 캐시가 남지 않게 원자적으로 교체
            with open(tmp_path, "w", encoding="utf-8") as fp:
                json.dump({"model": self.model, "content": content}, fp, ensure_ascii=False)
            os.replace(tmp_path, cache_path)
            print(
                f"[llm-cache] miss (hits={_CACHE_STATS['hits']}, misses={_CACHE_STATS['misses']})",
                file=sys.stderr,
            )
        return content

    def _create_with_retry(self, prompt: str, kwargs: dict) -> str:
        """호출 간격을 LLM_MAX_RPM에 맞추고, 일시적 오류는 지수 백오프로 그 자리에서 재시도한다.

        대기: 10·20·40·80·120·120초(상한 120초). 서버가 더 긴 Retry-After를 주면 그 값.
        LLM_TRANSIENT_RETRIES번을 넘기면 마지막 오류를 그대로 던진다.
        """
        for attempt in range(self.transient_retries + 1):
            _RATE_LIMITER.wait(self.max_rpm)
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    **kwargs,
                )
            except _TRANSIENT_ERRORS as exc:
                if attempt >= self.transient_retries:
                    raise
                delay = max(_retry_after_seconds(exc) or 0.0, min(_BACKOFF_CAP_SECONDS, 10.0 * 2**attempt))
                print(
                    f"[llm] {type(exc).__name__} — {delay:.0f}s 후 재시도 "
                    f"({attempt + 1}/{self.transient_retries})",
                    file=sys.stderr,
                )
                time.sleep(delay)
                continue
            content = response.choices[0].message.content
            if content is None:
                raise ValueError("LLM response did not include message content.")
            return content
        raise AssertionError("unreachable")

    def _cache_path(self, prompt: str, kwargs: dict) -> str | None:
        """LLM 응답 디스크 캐시 경로. LLM_CACHE_DIR 설정 시에만 캐시가 켜진다(기본 OFF).

        키 = (model, prompt, kwargs 전체)의 해시. 전제: 평가 파이프라인의 모든 호출이
        temperature=0 + response_format=json_object라 응답이 실질적으로 결정적이다 —
        다른 파라미터 조합은 kwargs가 키에 포함되므로 캐시가 오염되지 않는다.
        """
        cache_dir = os.getenv("LLM_CACHE_DIR")
        if not cache_dir:
            return None
        key_src = json.dumps(
            {"model": self.model, "prompt": prompt, "kwargs": kwargs},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        digest = hashlib.sha256(key_src.encode("utf-8")).hexdigest()
        return os.path.join(cache_dir, f"{digest}.json")

    def request_json(
        self,
        prompt: str,
        *,
        required_keys: set[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        raw = self.request(prompt, response_format={"type": "json_object"}, **kwargs)
        data = parse_json_object(raw)
        if required_keys:
            missing = required_keys - set(data)
            if missing:
                raise ValueError(f"LLM response is missing required keys: {sorted(missing)}")
        return data


def load_decision_score(decision: dict) -> float:
    """LLM 배정 판단 결과에서 score 필드를 안전하게 읽는다."""
    try:
        return float(decision.get("score", 0.0))
    except (TypeError, ValueError):
        return 0.0


def parse_json_object(value: str) -> dict:
    """모델 응답에서 JSON 객체를 파싱한다.

    일부 모델은 `response_format`을 줘도 앞뒤에 설명 문장을 붙일 수 있으므로,
    순수 JSON 파싱 실패 시 가장 바깥 `{...}` 구간만 한 번 더 시도한다.
    """
    try:
        data = json.loads(value, strict=False)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end < start:
            raise
        data = json.loads(value[start : end + 1], strict=False)
    if not isinstance(data, dict):
        raise ValueError("LLM response must be a JSON object.")
    return data


def _load_timeout_seconds() -> float:
    raw_value = os.getenv("LLM_TIMEOUT_SECONDS") or os.getenv("OPENAI_TIMEOUT_SECONDS") or "60"
    try:
        timeout = float(raw_value)
    except ValueError as exc:
        raise ValueError("LLM_TIMEOUT_SECONDS must be a number.") from exc
    if timeout <= 0:
        raise ValueError("LLM_TIMEOUT_SECONDS must be greater than 0.")
    return timeout


def _load_provider_only() -> list[str]:
    raw_value = os.getenv("LLM_PROVIDER_ONLY")
    if raw_value is None:
        return list(OPENROUTER_PROVIDER_ONLY)
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def _load_allow_fallbacks() -> bool:
    raw_value = os.getenv("LLM_PROVIDER_ALLOW_FALLBACKS")
    if raw_value is None:
        return OPENROUTER_ALLOW_FALLBACKS
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _retry_after_seconds(exc: Exception) -> float | None:
    """서버 응답의 Retry-After(초)를 읽는다. 없거나 숫자가 아니면 None."""
    response = getattr(exc, "response", None)
    raw = response.headers.get("retry-after") if response is not None else None
    try:
        return min(_BACKOFF_CAP_SECONDS, max(0.0, float(raw))) if raw else None
    except ValueError:
        return None


def _load_max_rpm() -> float:
    """LLM 호출 상한(분당). 기본 10: 9/24 실측에서 분당 약 8~11건일 때 429가 320건 중 3건,
    약 13~20건일 때 350여 건 중 41건이었다. 0이면 제한하지 않는다."""
    raw_value = os.getenv("LLM_MAX_RPM") or "10"
    try:
        rpm = float(raw_value)
    except ValueError as exc:
        raise ValueError("LLM_MAX_RPM must be a number.") from exc
    if rpm < 0:
        raise ValueError("LLM_MAX_RPM must be greater than or equal to 0.")
    return rpm


def _load_transient_retries() -> int:
    raw_value = os.getenv("LLM_TRANSIENT_RETRIES") or "6"
    try:
        retries = int(raw_value)
    except ValueError as exc:
        raise ValueError("LLM_TRANSIENT_RETRIES must be an integer.") from exc
    if retries < 0:
        raise ValueError("LLM_TRANSIENT_RETRIES must be greater than or equal to 0.")
    return retries

