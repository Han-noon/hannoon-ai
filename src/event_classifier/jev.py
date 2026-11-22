"""Jev(TypeSafe System One) 기반 이벤트 배정 판단.

후보 이벤트 전체를 한 상태(state)에 넣고, 후보마다 "새 기사와 같은 구체적 이벤트인가"를
확률(noul)로 받는다. 최고 확률이 임계값 이상이면 그 후보에 assign, 아니면 create.
Jev는 확률만 돌려주며 assign/create 결정은 이 모듈의 규칙이다.
(근거: pipeline/docs/이벤트_분류_배정판단_Jev_전환_결정안_2026-09-24.md, 313건 5회 반복 실험)
"""
from __future__ import annotations

import os
import time
from typing import Callable

import requests

from event_classifier.settings import JEV_ASSIGN_THRESHOLD, JEV_MODEL, JEV_URL

ProbsFn = Callable[[str, dict], dict]

_QUESTION = (
    "The [새 기사] (new article) and the [후보 이벤트 {i}] (candidate event {i}) describe the same specific event: "
    "subject, core action, procedural stage and date must substantively match; merely sharing the same topic is not enough."
)


def require_api_key() -> str:
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise RuntimeError("TYPESAFE_API_KEY가 없습니다. .env에 Jev API 키를 설정하세요.")
    return key


def build_state(title: str, published_date: str, major: str, candidates: list[dict]) -> str:
    parts = [f"[새 기사]\n제목: {title}\n게시일: {published_date}\n핵심 사건: {major}\n"]
    for i, c in enumerate(candidates, 1):
        parts.append(
            f"[후보 이벤트 {i}]\n제목: {c.get('title') or ''}\n이벤트 최초 사건: {c.get('core_content') or ''}\n"
            f"후보 이벤트의 멤버 기사(게시일 {str(c.get('source_published_at') or '')[:10]}) 사건: "
            f"{c.get('source_core_content') or ''}\n"
        )
    return "\n".join(parts)


def build_questions(n: int) -> dict:
    return {f"cand_{i}": _QUESTION.format(i=i) for i in range(1, n + 1)}


def request_probs(state: str, questions: dict) -> dict:
    """Jev 호출. 429/529·네트워크 오류는 지수 백오프로 재시도하고, 끝내 실패하면 예외를 던진다."""
    key = require_api_key()
    body = {
        "state": state,
        "model": JEV_MODEL,
        "questions": {k: {"type": "noul", "instructions": v} for k, v in questions.items()},
    }
    last = None
    for attempt in range(6):
        try:
            r = requests.post(JEV_URL, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=60)
            if r.status_code in (429, 529):
                last = f"HTTP {r.status_code}"
                time.sleep(2**attempt)
                continue
            if r.status_code != 200:
                raise RuntimeError(f"Jev HTTP {r.status_code}: {r.text[:150]}")
            return {k: v.get("noul") for k, v in r.json()["answers"].items()}
        except requests.RequestException as exc:
            last = str(exc)[:120]
            time.sleep(1 + attempt)
    raise RuntimeError(f"Jev 호출 실패: {last}")


def decide_assignment(
    title: str,
    published_date: str,
    major: str,
    candidates: list[dict],
    threshold: float = JEV_ASSIGN_THRESHOLD,
    probs_fn: ProbsFn | None = None,
) -> tuple[dict, dict]:
    """(decision, info)를 돌려준다. 확률이 하나라도 없으면 예외(조용히 create하지 않는다)."""
    if not candidates:
        raise ValueError("후보 없이 Jev 배정 판단을 호출했습니다.")
    answers = (probs_fn or request_probs)(
        build_state(title, published_date, major, candidates), build_questions(len(candidates))
    )
    probs = []
    for i in range(1, len(candidates) + 1):
        p = answers.get(f"cand_{i}")
        if p is None:
            raise RuntimeError(f"Jev 응답에 cand_{i} 확률이 없습니다.")
        probs.append(float(p))
    best = max(range(len(probs)), key=lambda i: probs[i])  # 동점이면 검색 순위가 앞선 후보
    info = {"probs": [round(p, 3) for p in probs], "best_index": best, "best_p": probs[best], "threshold": threshold}
    if probs[best] >= threshold:
        decision = {
            "action": "assign",
            "event_id": int(candidates[best]["id"]),
            "score": round(probs[best], 3),
            "reason": f"jev p={probs[best]:.2f}",
        }
    else:
        decision = {
            "action": "create",
            "event_title": title,
            "score": round(probs[best], 3),
            "reason": f"jev max p={probs[best]:.2f}<{threshold}",
        }
    return decision, info
