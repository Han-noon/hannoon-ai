from __future__ import annotations

import re


SUMMARY_MAX_SENTENCES = 4
SUMMARY_MAX_CHARS = 700
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?\u3002\uff01\uff1f])\s+")


def clean_string(value) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def normalize_summary(
    value,
    *,
    max_sentences: int = SUMMARY_MAX_SENTENCES,
    max_chars: int = SUMMARY_MAX_CHARS,
) -> str:
    text = clean_string(value)
    if not text:
        return ""

    if max_sentences > 0:
        sentences = [
            part.strip()
            for part in _SENTENCE_BOUNDARY_RE.split(text)
            if part.strip()
        ]
        if sentences:
            text = " ".join(sentences[:max_sentences])

    if max_chars <= 0 or len(text) <= max_chars:
        return text

    clipped = text[:max_chars].rstrip()
    boundary = max(
        clipped.rfind(". "),
        clipped.rfind("! "),
        clipped.rfind("? "),
        clipped.rfind("\u3002"),
        clipped.rfind("\uff01"),
        clipped.rfind("\uff1f"),
    )
    if boundary >= max_chars // 2:
        return clipped[: boundary + 1].rstrip()
    return clipped[: max_chars - 3].rstrip() + "..."
