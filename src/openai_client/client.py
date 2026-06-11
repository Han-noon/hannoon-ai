from __future__ import annotations

import json
import os
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI


DEFAULT_LLM_BASE_URL = "https://api.upstage.ai/v1"
DEFAULT_LLM_MODEL = "solar-mini"


class LLMClient:
    """OpenAI 호환 Chat Completions 클라이언트.

    기본 대상은 Upstage다. 환경 변수 이름은 의도적으로 범용 이름을 유지해
    나중에 OpenAI 또는 다른 호환 엔드포인트로 바꿔도 호출부를 고치지 않게 한다.
    """

    def __init__(
        self,
        model: str = DEFAULT_LLM_MODEL,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
    ) -> None:
        load_dotenv(override=True)
        self.model = model
        self.timeout = timeout if timeout is not None else _load_timeout_seconds()
        self.max_retries = _load_max_retries()

        resolved_api_key = (
            api_key
            or os.getenv("LLM_API_KEY")
            or os.getenv("UPSTAGE_API_KEY")
            or os.getenv("OPENAI_API_KEY")
        )
        if not resolved_api_key:
            raise ValueError(
                "Set LLM_API_KEY or UPSTAGE_API_KEY before calling the LLM pipeline."
            )

        resolved_base_url = (
            base_url
            or os.getenv("LLM_BASE_URL")
            or os.getenv("UPSTAGE_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or DEFAULT_LLM_BASE_URL
        )

        self._client = OpenAI(
            api_key=resolved_api_key,
            base_url=resolved_base_url,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )

    def request(self, prompt: str, **kwargs: Any) -> str:
        response = self._client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
        content = response.choices[0].message.content
        if content is None:
            raise ValueError("LLM response did not include message content.")
        return content

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


def parse_json_object(value: str) -> dict:
    """모델 응답에서 JSON 객체를 파싱한다.

    일부 모델은 `response_format`을 줘도 앞뒤에 설명 문장을 붙일 수 있으므로,
    순수 JSON 파싱 실패 시 가장 바깥 `{...}` 구간만 한 번 더 시도한다.
    """
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end < start:
            raise
        data = json.loads(value[start : end + 1])
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


def _load_max_retries() -> int:
    raw_value = os.getenv("LLM_MAX_RETRIES") or os.getenv("OPENAI_MAX_RETRIES") or "0"
    try:
        max_retries = int(raw_value)
    except ValueError as exc:
        raise ValueError("LLM_MAX_RETRIES must be an integer.") from exc
    if max_retries < 0:
        raise ValueError("LLM_MAX_RETRIES must be greater than or equal to 0.")
    return max_retries
