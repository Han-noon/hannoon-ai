from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class EventRow:
    id: int
    topic_id: int
    topic_name: str
    title: str
    created_at: datetime
    updated_at: datetime
    subtopic_processed_at: datetime | None


@dataclass(frozen=True)
class ArticleRow:
    id: int
    title: str
    publisher: str
    published_at: datetime
    link: str
    content: str
    updated_at: datetime | None = None


@dataclass(frozen=True)
class PreparedEvent:
    event: EventRow
    profile_input: dict[str, Any]
    input_hash: str
    prompt_version: str
    order_key: tuple[Any, ...]
    article_span_days: float


@dataclass(frozen=True)
class ProfileResult:
    event_id: int
    event_summary: str
    anchors: list[dict[str, Any]]
    input_hash: str
    prompt_version: str
    runtime_profile: dict[str, Any]
    reused: bool
