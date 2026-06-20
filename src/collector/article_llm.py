from __future__ import annotations

from dataclasses import dataclass

from openai_client.client import LLMClient
from summary_utils import clean_string, normalize_summary

from .settings import (
    DEFAULT_LLM_ABUSE_MODEL,
    DEFAULT_LLM_ABUSE_ENABLED,
    DEFAULT_LLM_ARTICLE_MODEL,
    DEFAULT_LLM_SUMMARY_MODEL,
)


@dataclass(frozen=True)
class ArticleAnalysis:
    """기사 단위 LLM 분석 결과."""

    summary: str
    abuse_label: str
    abuse_score: float
    abuse_reason: str
    keywords: list[str]


class ArticleLLMAnalyzer:
    """기사 요약과 어뷰징 판단을 LLM으로 수행한다."""

    def __init__(
        self,
        *,
        article_model: str = DEFAULT_LLM_ARTICLE_MODEL,
        abuse_model: str = DEFAULT_LLM_ABUSE_MODEL,
        summary_model: str = DEFAULT_LLM_SUMMARY_MODEL,
        abuse_enabled: bool = DEFAULT_LLM_ABUSE_ENABLED,
    ) -> None:
        self.article_model = article_model
        self.abuse_model = abuse_model
        self.summary_model = summary_model
        self.abuse_enabled = abuse_enabled
        self._clients: dict[str, LLMClient] = {}

    def analyze(
        self,
        *,
        title: str,
        subtitle: str,
        category: str,
        content: str,
    ) -> ArticleAnalysis:
        content = _clean_string(content)

        if self.abuse_enabled and self.abuse_model == self.summary_model:
            # 같은 모델을 쓸 때는 한 번의 호출로 비용과 지연 시간을 줄인다.
            return self._combined_analysis(
                model=self.abuse_model or self.article_model,
                title=title,
                subtitle=subtitle,
                category=category,
                content=content,
            )

        summary = self._summarize(title=title, category=category, content=content)
        if self.abuse_enabled:
            abuse_label, abuse_score, abuse_reason = self._classify_abuse(
                title=title,
                subtitle=subtitle,
                category=category,
                content=content,
                summary=summary,
            )
        else:
            abuse_label, abuse_score, abuse_reason = (
                "normal",
                0.0,
                "abuse_classification_skipped",
            )
        return ArticleAnalysis(
            summary=summary,
            abuse_label=abuse_label,
            abuse_score=abuse_score,
            abuse_reason=abuse_reason,
            keywords=[],
        )

    def _client(self, model: str) -> LLMClient:
        if model not in self._clients:
            self._clients[model] = LLMClient(model=model)
        return self._clients[model]

    def _combined_analysis(
        self,
        *,
        model: str,
        title: str,
        subtitle: str,
        category: str,
        content: str,
    ) -> ArticleAnalysis:
        prompt = f"""다음 한국어 뉴스 기사를 분석하세요.

아래 JSON 객체만 반환하세요:
{{
  "summary": "중립적인 한국어 요약 4문장",
  "abuse_label": "abuse 또는 normal",
  "abuse_score": 0.0,
  "abuse_reason": "짧은 한국어 판단 근거"
}}

규칙:
- 기사 본문에 있는 사실만 사용하고 새로운 사실을 만들지 마세요.
- 본문은 1차 정제됐지만 광고, 구독 유도, 내비게이션, 관련 기사, 저작권,
  공유 버튼, 기자 프로필 문구가 남아 있으면 무시하세요.
- abuse는 제목/본문이 오해를 유도하거나 낚시성이거나 서로 맞지 않거나,
  근거 없이 과도하게 자극적이거나, 본문 의미를 의도적으로 왜곡하는 경우입니다.
- normal은 제목과 본문이 일관된 일반 보도인 경우입니다.
- abuse_score는 0 이상 1 이하 숫자여야 합니다.
- summary는 기본 4문장, 전체 400~700자 정도로 작성하세요.
- summary 1문장은 핵심 사건/변화(누가, 무엇을 했는지)를 담으세요.
- summary 2문장은 배경/원인/맥락을 담으세요.
- summary 3문장은 현재 결과/상태/수치/영향을 담으세요.
- summary 4문장은 후속 쟁점/반응/예정된 절차를 담으세요.
- 경제, 국제, 사회, 정치 등 모든 카테고리에 같은 구조를 적용하되, 경제 기사는 기업·시장·수치·정책 영향, 국제 기사는 국가·기관·외교/안보 맥락, 사회 기사는 피해·기관 조치·제도 쟁점을 우선 포함하세요.
- summary는 나중에 이벤트/토픽 임베딩에 사용할 수 있도록 사건명, 주요 주체, 대상, 지역, 날짜, 수치, 상태 변화를 가능한 한 명시하세요.

제목: {title}
RSS 요약: {subtitle}
카테고리: {category}

기사 본문:
{content}
"""
        data = self._client(model).request_json(
            prompt,
            required_keys={"summary", "abuse_label", "abuse_score", "abuse_reason"},
            temperature=0,
        )
        return _normalize_analysis(data)

    def _summarize(self, *, title: str, category: str, content: str) -> str:
        prompt = f"""다음 한국어 뉴스 기사를 요약하세요.

아래 JSON 객체만 반환하세요:
{{"summary": "중립적인 한국어 요약 4문장"}}

규칙:
- 기사 본문에 있는 사실만 사용하세요.
- 광고, 관련 기사, 저작권, 기자 프로필 문구는 요약에 포함하지 마세요.
- 사실 중심의 중립적인 문장으로 작성하세요.
- 기본 4문장, 전체 400~650자 정도로 작성하세요.
- 1문장은 핵심 사건/변화(누가, 무엇을 했는지), 2문장은 배경/원인/맥락, 3문장은 현재 결과/상태/수치/영향, 4문장은 후속 쟁점/반응/예정된 절차를 담으세요.
- 경제, 국제, 사회, 정치 등 모든 카테고리에 같은 구조를 적용하되, 경제 기사는 기업·시장·수치·정책 영향, 국제 기사는 국가·기관·외교/안보 맥락, 사회 기사는 피해·기관 조치·제도 쟁점을 우선 포함하세요.
- 나중에 이벤트/토픽 임베딩에 사용할 수 있도록 사건명, 주요 주체, 대상, 지역, 날짜, 수치, 상태 변화를 가능한 한 명시하세요.

제목: {title}
카테고리: {category}
기사 본문:
{content}
"""
        data = self._client(self.summary_model).request_json(
            prompt,
            required_keys={"summary"},
            temperature=0,
        )
        summary = normalize_summary(data.get("summary"))
        if not summary:
            raise ValueError("LLM summary is empty.")
        return summary

    def _classify_abuse(
        self,
        *,
        title: str,
        subtitle: str,
        category: str,
        content: str,
        summary: str,
    ) -> tuple[str, float, str]:
        prompt = f"""다음 한국어 뉴스 기사가 어뷰징/오해 유도성 기사인지 판단하세요.

아래 JSON 객체만 반환하세요:
{{"abuse_label": "abuse 또는 normal", "abuse_score": 0.0, "abuse_reason": "짧은 한국어 판단 근거"}}

판단 기준:
- abuse: 제목이 낚시성이거나 오해를 유도함, 제목과 본문 불일치,
  근거 없는 자극적 프레이밍, 의도적 왜곡, 본문이 제목과 대부분 무관한 경우.
- normal: 제목과 본문이 일관된 일반 보도인 경우.
- abuse_score는 0 이상 1 이하 숫자여야 합니다.

제목: {title}
RSS 요약: {subtitle}
카테고리: {category}
생성된 요약: {summary}
기사 본문:
{content}
"""
        data = self._client(self.abuse_model).request_json(
            prompt,
            required_keys={"abuse_label", "abuse_score", "abuse_reason"},
            temperature=0,
        )
        label = _normalize_label(data.get("abuse_label"))
        score = _normalize_score(data.get("abuse_score"))
        reason = _clean_string(data.get("abuse_reason")) or "llm_classification"
        return label, score, reason

def _normalize_analysis(data: dict) -> ArticleAnalysis:
    summary = normalize_summary(data.get("summary"))
    if not summary:
        raise ValueError("LLM summary is empty.")
    return ArticleAnalysis(
        summary=summary,
        abuse_label=_normalize_label(data.get("abuse_label")),
        abuse_score=_normalize_score(data.get("abuse_score")),
        abuse_reason=_clean_string(data.get("abuse_reason")) or "llm_classification",
        keywords=[],
    )


def _normalize_label(value) -> str:
    label = str(value or "").strip().lower()
    if label not in {"abuse", "normal"}:
        raise ValueError(f"Invalid abuse_label from LLM: {value!r}")
    return label


def _normalize_score(value) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid abuse_score from LLM: {value!r}") from exc
    return max(0.0, min(1.0, score))


def _clean_string(value) -> str:
    return clean_string(value)
