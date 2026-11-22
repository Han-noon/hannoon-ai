"""이벤트 요약 롤업 (이벤트 멤버 기사 요약 → 이벤트 요약) + 이벤트 이름 생성.

rev3: 요약 롤업은 매 기사 assign이 아니라, 이벤트가 토픽 분류 대상(N개↑)이 되었을 때
토픽 분류 단계에서 1회 수행한다(단발성 이벤트에 롤업 LLM 낭비 방지).
이벤트 이름은 이벤트 생성 시 1회 만들고 이후 고정한다(다음 기사의 Jev 판단에 후보 제목으로 쓰임).
"""

from __future__ import annotations

import sys

from event_classifier.prompts import (
    build_event_summary_prompt,
    build_event_title_prompt,
)
from summary_utils import (
    EVENT_SUMMARY_MAX_SENTENCES,
    clean_string,
    normalize_summary,
    normalize_topic_title,
)


FETCH_EVENT_SUMMARY_ARTICLES_SQL = """
SELECT a.id AS article_id, a.title AS title, a.published_at AS published_at, r.summary AS summary
FROM event_articles ea
JOIN articles a ON a.id = ea.article_id
JOIN article_ai_results r ON r.article_id = a.id
WHERE ea.event_id = ?
  AND r.summary IS NOT NULL
  AND btrim(r.summary) <> ''
ORDER BY a.published_at ASC, a.id ASC
"""


def load_event_article_summaries(conn, event_id: int) -> list[dict]:
    return [
        {
            "article_id": row["article_id"],
            "title": row["title"],
            "published_at": str(row["published_at"] or "")[:16],
            "summary": normalize_summary(row["summary"]),
        }
        for row in conn.query(FETCH_EVENT_SUMMARY_ARTICLES_SQL, (event_id,))
        if normalize_summary(row["summary"])
    ]


def _event_summary(value) -> str:
    return normalize_summary(value, max_sentences=EVENT_SUMMARY_MAX_SENTENCES)


def generate_event_summary(
    client,
    *,
    event_title: str,
    article_summaries: list[dict],
    fallback_summary: str,
) -> tuple[str, str | None]:
    """멤버 기사 요약들을 (이벤트 요약, 짧은 명사형 요약)으로 롤업한다.

    1건 이하이거나 롤업이 실패하면 짧은 요약은 None(기존 값 유지)이다.
    """
    fallback = _event_summary(fallback_summary)
    if len(article_summaries) <= 1:
        return (_event_summary(article_summaries[0]["summary"]) if article_summaries else fallback), None
    try:
        data = client.request_json(
            build_event_summary_prompt(event_title, article_summaries),
            required_keys={"summary", "short_summary"},
            temperature=0,
        )
        summary = _event_summary(data.get("summary"))
        short_summary = _whole_phrase(data.get("short_summary"))
    except Exception as exc:
        print(f"[event-summary] rollup failed: {exc}", file=sys.stderr)
        summary, short_summary = "", ""
    return (summary or fallback), (short_summary or None)


def _whole_phrase(value) -> str:
    """이름·짧은 요약은 구 중간이 끊기지 않게 길이로 자르지 않고 끝 문장부호만 떼어낸다(길이는 프롬프트로 지시)."""
    return normalize_topic_title(value, max_chars=0)


def generate_event_title(client, *, article_title: str, major: str, published_at: str) -> str:
    """새 이벤트의 명사형 이름을 만든다(자르지 않음). 실패하거나 비면 기사 제목으로 대신한다."""
    fallback = clean_string(article_title)
    try:
        data = client.request_json(
            build_event_title_prompt(article_title, major, published_at),
            required_keys={"title"},
            temperature=0,
        )
        title = _whole_phrase(data.get("title"))
    except Exception as exc:
        print(f"[event-title] generation failed: {exc}", file=sys.stderr)
        title = ""
    return title or fallback
