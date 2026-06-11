from __future__ import annotations

import re
from pathlib import Path


class ExtractiveBertSummarizer:
    """KLUE BERT 기반 추출형 요약 모델로 중요한 문장을 선택한다."""

    def __init__(
        self,
        *,
        model_path: str | Path,
        tokenizer_dir: str | Path,
        device: str = "auto",
        max_length: int = 512,
        score_batch_size: int = 8,
    ) -> None:
        """로컬 BERTSUM weight와 KLUE BERT tokenizer/config를 로드한다."""
        if max_length <= 0:
            raise ValueError("max_length must be greater than 0.")
        if score_batch_size <= 0:
            raise ValueError("score_batch_size must be greater than 0.")

        self.model_path = Path(model_path)
        self.tokenizer_dir = Path(tokenizer_dir)
        self.max_length = max_length
        self.score_batch_size = score_batch_size

        try:
            import torch
            from transformers import AutoTokenizer, BertConfig, BertForSequenceClassification
        except ImportError as exc:
            raise RuntimeError(
                "요약 모델 실행에는 torch와 transformers가 필요합니다. requirements.txt를 설치하세요."
            ) from exc

        if not self.model_path.exists():
            raise FileNotFoundError(f"요약 모델 파일이 없습니다: {self.model_path}")
        if not self.tokenizer_dir.exists():
            raise FileNotFoundError(f"요약 tokenizer 디렉터리가 없습니다: {self.tokenizer_dir}")

        self._torch = torch
        self.device = self._resolve_device(device)
        self.tokenizer = AutoTokenizer.from_pretrained(
            self.tokenizer_dir,
            local_files_only=True,
            use_fast=True,
        )

        config = BertConfig.from_pretrained(self.tokenizer_dir, local_files_only=True)
        config.num_labels = 1
        self.model = BertForSequenceClassification(config)
        state_dict = torch.load(self.model_path, map_location="cpu", weights_only=True)
        missing, unexpected = self.model.load_state_dict(state_dict, strict=False)
        if missing or unexpected:
            raise RuntimeError(
                "요약 모델 weight가 BERT 분류 모델 구조와 맞지 않습니다. "
                f"missing={missing}, unexpected={unexpected}"
            )
        self.model.to(self.device)
        self.model.eval()

    def summarize(
        self,
        *,
        title: str,
        content: str,
        sentence_count: int = 3,
        max_candidates: int = 80,
        head_candidates: int = 50,
        middle_candidates: int = 15,
        tail_candidates: int = 15,
    ) -> str:
        """긴 기사를 문장 단위로 점수화해 상위 문장을 원문 순서대로 반환한다."""
        if sentence_count <= 0:
            raise ValueError("sentence_count must be greater than 0.")
        if max_candidates <= 0:
            raise ValueError("max_candidates must be greater than 0.")
        if head_candidates < 0 or middle_candidates < 0 or tail_candidates < 0:
            raise ValueError("candidate counts must be greater than or equal to 0.")
        if head_candidates + middle_candidates + tail_candidates <= 0:
            raise ValueError("at least one candidate count must be greater than 0.")
        if head_candidates + middle_candidates + tail_candidates > max_candidates:
            raise ValueError("head/middle/tail candidate counts must not exceed max_candidates.")

        sentences = _split_sentences(content)
        if not sentences:
            raise ValueError("요약할 본문 문장이 없습니다.")

        candidates = _limit_candidates(
            sentences,
            max_candidates=max_candidates,
            head_candidates=head_candidates,
            middle_candidates=middle_candidates,
            tail_candidates=tail_candidates,
        )
        scores = self._score_sentences(title=title, sentences=candidates)
        selected = sorted(
            sorted(enumerate(scores), key=lambda item: item[1], reverse=True)[:sentence_count],
            key=lambda item: item[0],
        )
        summary = " ".join(candidates[index] for index, _ in selected)
        if not summary:
            raise ValueError("요약 결과가 비어 있습니다.")
        return summary

    def _score_sentences(self, *, title: str, sentences: list[str]) -> list[float]:
        """문장 후보들을 배치로 나눠 모델 점수를 계산한다."""
        scores: list[float] = []
        title = _clean_text(title)
        sep = getattr(self.tokenizer, "sep_token", None) or "[SEP]"
        inputs = [
            f"{title} {sep} {sentence}" if title else sentence
            for sentence in sentences
        ]

        for start in range(0, len(inputs), self.score_batch_size):
            batch = inputs[start : start + self.score_batch_size]
            encoded = self.tokenizer(
                batch,
                padding=True,
                truncation=True,
                max_length=self.max_length,
                return_tensors="pt",
            )
            encoded = {key: value.to(self.device) for key, value in encoded.items()}
            with self._torch.no_grad():
                logits = self.model(**encoded).logits.squeeze(-1)
                probs = self._torch.sigmoid(logits)
            scores.extend(float(value) for value in probs.detach().cpu().tolist())

        return scores

    def _resolve_device(self, device: str) -> str:
        """auto 설정이면 CUDA 사용 가능 여부에 따라 추론 장치를 고른다."""
        if device == "auto":
            return "cuda" if self._torch.cuda.is_available() else "cpu"
        return device


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+|(?<=[.!?。！？])(?=[가-힣A-Za-z0-9])")


def _split_sentences(text: str | None) -> list[str]:
    """본문을 문장 후보 목록으로 나눈다."""
    normalized = _clean_text(text)
    if not normalized:
        return []
    sentences = [
        sentence
        for sentence in (_clean_text(part) for part in _SENTENCE_SPLIT_RE.split(normalized))
        if sentence
    ]
    long_sentences = [sentence for sentence in sentences if len(sentence) >= 20]
    return long_sentences or sentences or [normalized]


def _limit_candidates(
    sentences: list[str],
    *,
    max_candidates: int,
    head_candidates: int,
    middle_candidates: int,
    tail_candidates: int,
) -> list[str]:
    """긴 기사에서 앞/중간/끝 문장을 균형 있게 후보로 뽑는다."""
    if len(sentences) <= max_candidates:
        return sentences

    indexes: set[int] = set()
    indexes.update(range(min(head_candidates, len(sentences))))
    indexes.update(range(max(len(sentences) - tail_candidates, 0), len(sentences)))

    if middle_candidates > 0:
        middle_center = len(sentences) // 2
        middle_start = max(middle_center - middle_candidates // 2, 0)
        middle_end = min(middle_start + middle_candidates, len(sentences))
        indexes.update(range(middle_start, middle_end))

    # 중복 구간이 생겨도 원문 순서를 보존해야 추출형 요약이 자연스럽다.
    return [sentences[index] for index in sorted(indexes)[:max_candidates]]


def _clean_text(value: str | None) -> str:
    """빈 값과 과도한 공백을 정리한다."""
    if not value:
        return ""
    return " ".join(str(value).split())
