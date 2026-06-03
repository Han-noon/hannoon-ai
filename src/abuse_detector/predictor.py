from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ModelDecision:
    """단일 모델의 점수, 임계값, 라벨 판정 결과를 담는다."""

    model_name: str
    positive_label: str
    score: float
    threshold: float
    is_abuse: bool

    @property
    def label(self) -> str:
        """모델 점수를 사람이 읽기 쉬운 라벨로 반환한다."""
        return "abuse" if self.is_abuse else "normal"

    def to_dict(self) -> dict[str, Any]:
        """로그나 디버깅에 쓰기 쉬운 dict 형태로 변환한다."""
        return {
            "model_name": self.model_name,
            "positive_label": self.positive_label,
            "score": self.score,
            "threshold": self.threshold,
            "label": self.label,
        }


@dataclass(frozen=True)
class TextInput:
    """모델 입력 문자열을 만들기 위한 기사 텍스트 필드 모음이다."""

    title: str
    subtitle: str
    category: str
    content: str


class LocalSequenceClassifier:
    """로컬 Hugging Face sequence classification 모델을 로드해 추론한다."""

    def __init__(
        self,
        model_dir: str | Path,
        *,
        model_name: str,
        positive_label: str,
        device: str = "auto",
    ) -> None:
        """모델 디렉터리의 설정, tokenizer, weight를 로컬 파일에서 로드한다."""
        self.model_dir = Path(model_dir)
        self.model_name = model_name
        self.positive_label = positive_label

        self.run_config = self._load_json("run_config.json")
        self.threshold_config = self._load_json("threshold.json")
        self.threshold = float(self.threshold_config["threshold"])
        self.max_length = int(self.run_config.get("max_length") or 512)
        self.content_sentences = int(self.run_config.get("content_sentences") or 16)
        self.text_mode = str(self.run_config.get("text_mode") or "")

        try:
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                "Abuse classification requires torch and transformers. "
                "Install requirements.txt before running classify."
            ) from exc

        self._torch = torch
        self.device = self._resolve_device(device)
        self.tokenizer = AutoTokenizer.from_pretrained(
            self.model_dir,
            local_files_only=True,
            use_fast=True,
        )
        self.model = AutoModelForSequenceClassification.from_pretrained(
            self.model_dir,
            local_files_only=True,
        )
        self.model.to(self.device)
        self.model.eval()
        self.positive_label_id = self._find_label_id(positive_label)

    def predict(self, article: TextInput) -> ModelDecision:
        """기사 입력을 모델 텍스트로 변환한 뒤 점수와 임계값으로 라벨을 결정한다."""
        text = self._build_text(article)
        encoded = self.tokenizer(
            text,
            truncation=True,
            max_length=self.max_length,
            return_tensors="pt",
        )
        encoded = {key: value.to(self.device) for key, value in encoded.items()}

        with self._torch.no_grad():
            logits = self.model(**encoded).logits[0]

        score = self._positive_score(logits)
        return ModelDecision(
            model_name=self.model_name,
            positive_label=self.positive_label,
            score=score,
            threshold=self.threshold,
            is_abuse=score >= self.threshold,
        )

    def _load_json(self, file_name: str) -> dict[str, Any]:
        """모델 디렉터리에 포함된 JSON 메타데이터 파일을 읽는다."""
        path = self.model_dir / file_name
        if not path.exists():
            raise FileNotFoundError(f"Missing model metadata file: {path}")
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError(f"{path} must contain a JSON object.")
        return data

    def _resolve_device(self, device: str) -> str:
        """auto 설정이면 CUDA 사용 가능 여부에 따라 추론 장치를 고른다."""
        if device == "auto":
            return "cuda" if self._torch.cuda.is_available() else "cpu"
        return device

    def _find_label_id(self, label_name: str) -> int:
        """모델 config에서 어뷰징으로 볼 positive label의 id를 찾는다."""
        label2id = getattr(self.model.config, "label2id", None) or {}
        if label_name in label2id:
            return int(label2id[label_name])

        id2label = getattr(self.model.config, "id2label", None) or {}
        for label_id, value in id2label.items():
            if str(value) == label_name:
                return int(label_id)

        available = ", ".join(str(value) for value in id2label.values())
        raise ValueError(
            f"Positive label '{label_name}' was not found in {self.model_dir}. "
            f"Available labels: {available}"
        )

    def _positive_score(self, logits) -> float:
        """모델 logits에서 positive label에 해당하는 확률 점수를 계산한다."""
        problem_type = getattr(self.model.config, "problem_type", None)
        if problem_type == "multi_label_classification":
            probs = self._torch.sigmoid(logits)
        else:
            probs = self._torch.softmax(logits, dim=-1)
        return float(probs[self.positive_label_id].detach().cpu().item())

    def _build_text(self, article: TextInput) -> str:
        """학습 설정에 맞춰 기사 필드를 하나의 모델 입력 문자열로 합친다."""
        content = _first_sentences(article.content, self.content_sentences)
        if self.text_mode.startswith("structured_"):
            return "\n".join(
                [
                    f"headline: {_clean_text(article.title)}",
                    f"subtitle: {_clean_text(article.subtitle)}",
                    f"category: {_clean_text(article.category)}",
                    f"content: {content}",
                ]
            )

        sep = getattr(self.tokenizer, "sep_token", None) or "[SEP]"
        parts = [
            _clean_text(article.title),
            _clean_text(article.subtitle),
            _clean_text(article.category),
            content,
        ]
        return f" {sep} ".join(part for part in parts if part)


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _clean_text(value: str | None) -> str:
    """빈 값과 과도한 공백을 정리한다."""
    if not value:
        return ""
    return " ".join(str(value).split())


def _first_sentences(value: str | None, limit: int) -> str:
    """본문이 너무 길 때 앞쪽 문장만 제한해서 사용한다."""
    text = _clean_text(value)
    if not text or limit <= 0:
        return text
    sentences = [part.strip() for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]
    if len(sentences) <= limit:
        return text
    return " ".join(sentences[:limit])
