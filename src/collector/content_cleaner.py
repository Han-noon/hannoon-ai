import json
from typing import Protocol

from .settings import DEFAULT_LLM_CLEANUP_MODEL


class CompletionClient(Protocol):
    def request(self, prompt: str, **kwargs) -> str:
        ...


BOILERPLATE_KEYWORDS = (
    "광고",
    "구독",
    "앱에서 보기",
    "관련기사",
    "관련 기사",
    "많이 본 뉴스",
    "인기뉴스",
    "제보",
    "무단전재",
    "재배포 금지",
    "저작권자",
    "copyright",
    "all rights reserved",
    "페이스북",
    "카카오톡",
    "url 복사",
    "공유하기",
    "댓글",
    "뉴스레터",
    "알림 받기",
    "기자 페이지",
)

STRONG_BOILERPLATE_KEYWORDS = (
    "무단전재",
    "재배포 금지",
    "copyright",
    "all rights reserved",
    "url 복사",
    "앱에서 보기",
)


PROMPT_TEMPLATE = """You clean extracted news article text.

Return JSON only in this exact shape:
{{"content": "cleaned article text"}}

Rules:
- Keep the original article facts, order, names, dates, quotes, and meaning.
- Do not summarize, translate, rewrite, add new facts, or add commentary.
- Remove advertising, sponsorship blocks, subscription prompts, app download prompts,
  newsletter prompts, social sharing labels, navigation text, related-article lists,
  copyright/footer text, reporter profile blurbs, and nonessential photo captions.
- If a sentence could be part of the article body, keep it.
- If the input is already clean, return it unchanged.
- Preserve the original language of the article.

Article text:
{text}
"""


class ArticleTextCleaner:
    def __init__(self, model: str | None = None):
        from openai_client.client import OpenAIClient

        self._client = OpenAIClient(model=model or DEFAULT_LLM_CLEANUP_MODEL)

    def clean(self, text: str) -> str:
        return clean_article_text(text, self._client)


def should_llm_cleanup(text: str) -> tuple[bool, list[str]]:
    """Return whether extracted text is suspicious enough to spend LLM tokens."""
    normalized = " ".join(text.split())
    if not normalized:
        return False, []

    reasons: list[str] = []
    lower_text = normalized.lower()

    if len(normalized) > 8000:
        reasons.append("too_long")

    keyword_hits = [
        keyword
        for keyword in BOILERPLATE_KEYWORDS
        if keyword.lower() in lower_text
    ]
    if len(keyword_hits) >= 2:
        reasons.append("boilerplate_keywords:" + ",".join(keyword_hits[:5]))

    strong_hits = [
        keyword
        for keyword in STRONG_BOILERPLATE_KEYWORDS
        if keyword.lower() in lower_text
    ]
    if strong_hits:
        reasons.append("strong_boilerplate:" + ",".join(strong_hits[:3]))

    related_count = normalized.count("관련기사") + normalized.count("관련 기사")
    if related_count >= 2:
        reasons.append("repeated_related_articles")

    share_count = sum(
        normalized.count(keyword)
        for keyword in ("공유하기", "페이스북", "카카오톡", "URL 복사", "url 복사")
    )
    if share_count >= 2:
        reasons.append("share_ui_noise")

    return bool(reasons), reasons


def clean_article_text(text: str, client: CompletionClient) -> str:
    prompt = PROMPT_TEMPLATE.format(text=text)
    response = client.request(
        prompt,
        temperature=0,
        response_format={"type": "json_object"},
    )
    data = _parse_json_object(response)
    cleaned = data.get("content")
    if not isinstance(cleaned, str):
        raise ValueError("LLM cleanup response is missing string field 'content'.")
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        raise ValueError("LLM cleanup returned empty content.")
    return cleaned


def _parse_json_object(value: str) -> dict:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start < 0 or end < start:
            raise
        return json.loads(value[start : end + 1])
