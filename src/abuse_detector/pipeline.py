from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .predictor import LocalSequenceClassifier, ModelDecision, TextInput


@dataclass(frozen=True)
class ArticleInput:
    """어뷰징 분류 모델에 넘길 기사 입력값이다."""

    title: str
    subtitle: str
    category: str
    content: str


@dataclass(frozen=True)
class AbuseDecision:
    """p1/p2 모델별 결과와 최종 어뷰징 판정 결과를 함께 담는다."""

    p1: ModelDecision
    p2: ModelDecision
    abuse_label: str
    abuse_score: float
    decision_reason: str

    @property
    def is_abuse(self) -> bool:
        """최종 라벨이 어뷰징인지 반환한다."""
        return self.abuse_label == "abuse"

    def to_dict(self) -> dict[str, Any]:
        """로그나 디버깅에 쓰기 쉬운 dict 형태로 변환한다."""
        return {
            "abuse_label": self.abuse_label,
            "abuse_score": self.abuse_score,
            "decision_reason": self.decision_reason,
            "p1": self.p1.to_dict(),
            "p2": self.p2.to_dict(),
        }


class ArticleAbuseDetector:
    """낚시성 모델과 주제분리 모델을 함께 실행해 최종 어뷰징 여부를 판단한다."""

    def __init__(
        self,
        *,
        p1_model_dir: str | Path,
        p2_model_dir: str | Path,
        device: str = "auto",
    ) -> None:
        """p1/p2 로컬 모델을 각각 로드한다."""
        self.p1 = LocalSequenceClassifier(
            p1_model_dir,
            model_name="p1_clickbait",
            positive_label="clickbait",
            device=device,
        )
        self.p2 = LocalSequenceClassifier(
            p2_model_dir,
            model_name="p2_topic_mismatch",
            positive_label="topic_mismatch",
            device=device,
        )

    def classify(self, article: ArticleInput) -> AbuseDecision:
        """두 모델 중 하나라도 어뷰징이면 최종 어뷰징으로 판단한다."""
        text_input = TextInput(
            title=article.title,
            subtitle=article.subtitle,
            category=article.category,
            content=article.content,
        )
        p1_decision = self.p1.predict(text_input)
        p2_decision = self.p2.predict(text_input)

        # 판정 사유는 로그에서 어떤 모델이 어뷰징으로 본 것인지 확인하기 위해 남긴다.
        reasons = []
        if p1_decision.is_abuse:
            reasons.append("p1_clickbait")
        if p2_decision.is_abuse:
            reasons.append("p2_topic_mismatch")

        abuse_label = "abuse" if reasons else "normal"
        decision_reason = "+".join(reasons) if reasons else "both_models_normal"
        abuse_score = max(p1_decision.score, p2_decision.score)

        return AbuseDecision(
            p1=p1_decision,
            p2=p2_decision,
            abuse_label=abuse_label,
            abuse_score=abuse_score,
            decision_reason=decision_reason,
        )
