"""LLM adapter for user-facing Subtopic summaries."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

from .profile import pipeline_child_env
from .settings import (
    DEFAULT_SUMMARY_MODEL,
    SUMMARY_PROMPT_NAME,
    prompt_path,
    validate_runtime_assets,
)


class SubtopicSummaryError(RuntimeError):
    pass


def _parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise
        value, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, dict):
        raise ValueError("summary response must be a JSON object")
    return value


def _provider_credentials() -> tuple[str, str]:
    env = pipeline_child_env()
    for prefix in ("QWEN", "OPENROUTER", "OPENAI"):
        key = env.get(f"{prefix}_API_KEY")
        base_url = env.get(f"{prefix}_BASE_URL")
        if key and base_url:
            return key, base_url
    raise SubtopicSummaryError(
        "A matching API key/base URL pair is required: QWEN_*, OPENROUTER_*, or OPENAI_*"
    )


class SubtopicSummaryGenerator:
    def __init__(self, *, env_file: Path, model: str | None = None) -> None:
        self.env_file = env_file.resolve()
        if not self.env_file.is_file():
            raise SubtopicSummaryError(f"environment file not found: {self.env_file}")
        load_dotenv(dotenv_path=self.env_file, override=False)
        validate_runtime_assets()
        self.model = (
            model
            or os.getenv("OPENROUTER_SUBTOPIC_SUMMARY_MODEL")
            or os.getenv("OPENROUTER_SUBTOPIC_MODEL")
            or os.getenv("OPENROUTER_MODEL")
            or os.getenv("QWEN_MODEL")
            or DEFAULT_SUMMARY_MODEL
        )
        self.system_prompt = prompt_path(SUMMARY_PROMPT_NAME).read_text(encoding="utf-8")
        api_key, base_url = _provider_credentials()
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.max_attempts = 3

    def generate(self, item: dict[str, Any]) -> str:
        if not item.get("events"):
            raise SubtopicSummaryError("cannot summarize a Subtopic without Events")

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": self.system_prompt},
                        {
                            "role": "user",
                            "content": json.dumps(item, ensure_ascii=False, indent=2),
                        },
                    ],
                    temperature=0.1,
                    max_tokens=700,
                    response_format={"type": "json_object"},
                )
                content = response.choices[0].message.content
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("summary model returned empty content")
                raw = _parse_json_object(content)
                summary = raw.get("summary")
                if not isinstance(summary, str) or not summary.strip():
                    raise ValueError("summary response has no non-empty 'summary' field")
                return " ".join(summary.split())
            except Exception as exc:
                last_error = exc
                if attempt < self.max_attempts:
                    time.sleep(float(attempt))

        raise SubtopicSummaryError(
            f"Subtopic summary generation failed after {self.max_attempts} attempts: {last_error}"
        ) from last_error
