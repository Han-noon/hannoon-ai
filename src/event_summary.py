"""이벤트 이름 생성.

이벤트 이름은 이벤트 생성 시 1회 만들고 이후 고정한다(다음 기사의 Jev 판단에 후보 제목으로 쓰임).
"""

from __future__ import annotations

import sys

from event_classifier.prompts import build_event_title_prompt
from summary_utils import clean_string, normalize_topic_title


def _whole_phrase(value) -> str:
    """이름은 구 중간이 끊기지 않게 길이로 자르지 않고 끝 문장부호만 떼어낸다(길이는 프롬프트로 지시)."""
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
