from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .models import ArticleRow, EventRow, PreparedEvent, ProfileResult
from .settings import (
    DEFAULT_PROFILE_MODEL,
    PROFILE_PROMPT_NAME,
    PROFILE_RUNTIME_PATH,
    PROFILE_SCHEMA_NAME,
    prompt_path,
    schema_path,
    validate_runtime_assets,
)


class ProfileGenerationError(RuntimeError):
    pass


SEOUL = ZoneInfo("Asia/Seoul")


def pipeline_child_env() -> dict[str, str]:
    """Build a consistent key/base-url pair for the validated runtime scripts.

    The validated runners support several provider variable families independently;
    pin a matched key/base-url pair to QWEN_* so mixed environments stay deterministic.
    """
    child_env = os.environ.copy()
    if not (child_env.get("QWEN_API_KEY") and child_env.get("QWEN_BASE_URL")):
        for prefix in ("OPENROUTER", "OPENAI"):
            key = child_env.get(f"{prefix}_API_KEY")
            base_url = child_env.get(f"{prefix}_BASE_URL")
            if key and base_url:
                child_env["QWEN_API_KEY"] = key
                child_env["QWEN_BASE_URL"] = base_url
                break
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    return child_env


def effective_profile_model(model: str | None = None) -> str:
    """Resolve the model exactly like the validated Profile runner does."""

    return (
        model
        or os.getenv("OPENROUTER_MODEL")
        or os.getenv("QWEN_MODEL")
        or DEFAULT_PROFILE_MODEL
    )


def profile_prompt_version(model: str | None = None) -> str:
    """Cache version for the validated Profile prompt/schema/model tuple.

    The previous adapter only hashed prompt/schema, so switching the actual
    Profile model could silently reuse old cached output.  Keep the DB schema
    unchanged and include the effective model in ``prompt_version`` instead.
    """

    validate_runtime_assets()
    resolved_model = effective_profile_model(model)
    material = (
        prompt_path(PROFILE_PROMPT_NAME).read_bytes()
        + b"\0"
        + schema_path(PROFILE_SCHEMA_NAME).read_bytes()
        + b"\0model="
        + resolved_model.encode("utf-8")
    )
    return "17_77-event-profile:" + hashlib.sha256(material).hexdigest()[:16]


def _iso(value: datetime) -> str:
    return _to_seoul(value).isoformat(timespec="seconds")


def _to_seoul(value: datetime) -> datetime:
    """Match 17_78_prepare_event_profile_inputs.py timezone semantics.

    Supabase currently stores ``published_at`` as timestamp without time zone,
    so a naive value is interpreted as already being Asia/Seoul.  Aware values
    are converted to Asia/Seoul before representative-date calculation.
    """

    if value.tzinfo is None:
        return value.replace(tzinfo=SEOUL)
    return value.astimezone(SEOUL)


def prepare_event(
    event: EventRow,
    articles: list[ArticleRow],
    *,
    prompt_version: str | None = None,
) -> PreparedEvent:
    if not articles:
        raise ValueError(f"event_id={event.id}: 연결된 기사가 없습니다.")
    missing_content = [str(article.id) for article in articles if not article.content.strip()]
    if missing_content:
        raise ValueError(
            f"event_id={event.id}: articles.content가 비어 있는 기사: "
            + ", ".join(missing_content)
        )

    ordered = sorted(articles, key=lambda article: (_to_seoul(article.published_at), article.id))
    seoul_times = {article.id: _to_seoul(article.published_at) for article in ordered}
    date_counts = Counter(seoul_times[article.id].date().isoformat() for article in ordered)
    max_count = max(date_counts.values())
    representative_date = min(date for date, count in date_counts.items() if count == max_count)
    first = seoul_times[ordered[0].id]
    last = seoul_times[ordered[-1].id]
    resolved_prompt_version = prompt_version or profile_prompt_version()

    article_payload = [
        {
            "article_id": str(article.id),
            "article_url": article.link,
            "title": article.title,
            "publisher": article.publisher,
            "published_at": _iso(article.published_at),
            "content": article.content,
        }
        for article in ordered
    ]
    profile_input = {
        "topic_key": str(event.topic_id),
        "event_id": str(event.id),
        "event_name": event.title,
        "representative_published_date": representative_date,
        "first_published_at": _iso(first),
        "last_published_at": _iso(last),
        "article_count": len(article_payload),
        "published_date_counts": dict(sorted(date_counts.items())),
        "articles": article_payload,
    }
    hash_payload = {
        "event_id": str(event.id),
        "event_name": event.title,
        "topic_key": str(event.topic_id),
        "prompt_version": resolved_prompt_version,
        "articles": article_payload,
    }
    canonical = json.dumps(
        hash_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    content_hash = hashlib.sha256(canonical).hexdigest()
    # The prefix is SQL-comparable without a schema change. event_articles has
    # no updated_at column, so this catches both added and removed links during
    # the pending scan; the trailing SHA-256 still protects all actual inputs.
    article_ids = ",".join(
        str(article.id) for article in sorted(articles, key=lambda item: item.id)
    ).encode("ascii")
    article_set_hash = hashlib.md5(article_ids, usedforsecurity=False).hexdigest()
    input_hash = f"v2:{article_set_hash}:{content_hash}"
    span_days = (last - first).total_seconds() / 86400.0
    numeric_id: int | str = event.id
    order_key = (representative_date, _iso(first), numeric_id)
    return PreparedEvent(
        event=event,
        profile_input=profile_input,
        input_hash=input_hash,
        prompt_version=resolved_prompt_version,
        order_key=order_key,
        article_span_days=span_days,
    )


def from_cached_profile(prepared: PreparedEvent, row: dict[str, Any]) -> ProfileResult | None:
    if row.get("input_hash") != prepared.input_hash:
        return None
    if row.get("prompt_version") != prepared.prompt_version:
        return None
    event_summary = str(row.get("event_summary") or "").strip()
    anchors = row.get("anchors")
    if not event_summary or not isinstance(anchors, list):
        return None
    runtime_profile = {
        "event_id": str(prepared.event.id),
        "event_name": prepared.event.title,
        "representative_published_date": prepared.profile_input["representative_published_date"],
        "first_published_at": prepared.profile_input["first_published_at"],
        "event_summary": event_summary,
        "anchors": anchors,
    }
    return ProfileResult(
        event_id=prepared.event.id,
        event_summary=event_summary,
        anchors=anchors,
        input_hash=prepared.input_hash,
        prompt_version=prepared.prompt_version,
        runtime_profile=runtime_profile,
        reused=True,
    )


def generate_profile(
    prepared: PreparedEvent,
    *,
    env_file: Path,
    model: str | None,
    json_mode: bool,
) -> ProfileResult:
    validate_runtime_assets()
    with tempfile.TemporaryDirectory(prefix="hannoon-profile-") as temp_name:
        temp = Path(temp_name)
        input_path = temp / "input.jsonl"
        output_path = temp / "output.jsonl"
        input_path.write_text(
            json.dumps(prepared.profile_input, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        cmd = [
            sys.executable,
            str(PROFILE_RUNTIME_PATH),
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--prompt",
            str(prompt_path(PROFILE_PROMPT_NAME)),
            "--schema",
            str(schema_path(PROFILE_SCHEMA_NAME)),
            "--env-file",
            str(env_file),
            "--no-resume",
        ]
        if model:
            cmd += ["--model", model]
        if json_mode:
            cmd.append("--json-mode")
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=pipeline_child_env(),
        )
        if completed.returncode != 0:
            detail = (completed.stdout + "\n" + completed.stderr).strip()[-4000:]
            raise ProfileGenerationError(
                f"event_id={prepared.event.id}: Event Profile 생성 실패\n{detail}"
            )
        rows = [
            json.loads(line)
            for line in output_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ] if output_path.exists() else []
        if len(rows) != 1:
            failure_path = Path(str(output_path) + ".failures.jsonl")
            failure = failure_path.read_text(encoding="utf-8")[-4000:] if failure_path.exists() else ""
            raise ProfileGenerationError(
                f"event_id={prepared.event.id}: Profile 결과가 1건이 아닙니다. {failure}"
            )
        profile = rows[0]
        event_summary = str(profile.get("event_summary") or "").strip()
        anchors = profile.get("anchors")
        if not event_summary or not isinstance(anchors, list):
            raise ProfileGenerationError(
                f"event_id={prepared.event.id}: event_summary/anchors가 유효하지 않습니다."
            )
        return ProfileResult(
            event_id=prepared.event.id,
            event_summary=event_summary,
            anchors=anchors,
            input_hash=prepared.input_hash,
            prompt_version=prepared.prompt_version,
            runtime_profile=profile,
            reused=False,
        )
