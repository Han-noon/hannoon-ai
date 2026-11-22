"""후보 검색 진단 로그 (Phase 0 계측).

이벤트/토픽 분류의 "후보 검색 결과 + 최종 판정"을 JSONL로 남겨,
과분할 오류를 검색 미스형(정답이 후보에 없음) vs 판정 오판형(후보에 있는데 create)으로
분해하는 데 쓴다(.omc/plans/event-topic-accuracy-plan.md Phase 1a).

CANDIDATE_LOG_PATH 환경변수가 설정된 경우에만 기록한다.
기본 OFF — 미설정 시 완전 무동작이라 기존 파이프라인 동작에 영향이 없다.
"""

from __future__ import annotations

import datetime
import json
import os


def log_candidates(record: dict) -> None:
    """후보 검색·판정 레코드 1건을 JSONL로 append한다. 실패해도 분류를 막지 않는다."""
    path = os.getenv("CANDIDATE_LOG_PATH")
    if not path:
        return
    try:
        line = json.dumps(
            {"ts": datetime.datetime.now().isoformat(timespec="seconds"), **record},
            ensure_ascii=False,
            default=str,
        )
        with open(path, "a", encoding="utf-8") as fp:
            fp.write(line + "\n")
    except OSError as exc:  # 진단 로그는 부차 기능 — 분류 자체를 죽이지 않는다
        print(f"[diagnostics] candidate log write failed: {exc}")
