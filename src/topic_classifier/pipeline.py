from __future__ import annotations

import sys

from db import events, topics, topic_causes
from embedding import embed_passage, embed_query, to_vector_literal
from openai_client.client import LLMClient
from topic_classifier.prompts import (
    build_topic_assignment_prompt,
    build_topic_cause_result_prompt,
    build_topic_update_prompt,
)
from topic_classifier.settings import (
    ASSIGN_SCORE_THRESHOLD,
    DISTANCE_THRESHOLD,
    LLM_MODEL,
)


_client: LLMClient | None = None


def _get_client(model: str = LLM_MODEL) -> LLMClient:
    """같은 실행 안에서는 모델별 LLM 클라이언트를 재사용한다."""
    global _client
    if _client is None or _client.model != model:
        _client = LLMClient(model=model)
    return _client


def _call_json(client: LLMClient, prompt: str, required_keys: set[str]) -> dict:
    """LLM에 JSON 응답을 요청하고 필수 키를 검증한다."""
    data = client.request_json(prompt, required_keys=required_keys, temperature=0)
    return data


def _reason_rejects_assignment(reason: str | None) -> bool:
    """Detect contradictory assign decisions whose reason says the topic is unrelated."""
    normalized = " ".join(str(reason or "").split()).lower()
    if not normalized:
        return False
    negative_markers = (
        "무관",
        "관련성이 없어",
        "관련성이 없다",
        "연관성이 없어",
        "연관성이 없다",
        "직접적인 연관성이 없어",
        "직접적인 연관성이 없다",
        "일치하지 않",
        "후보가 없음",
        "새로운 사건",
        "새로운 사안",
    )
    return any(marker in normalized for marker in negative_markers)


def _load_decision_score(decision: dict) -> float:
    try:
        return float(decision.get("score", 0.0))
    except (TypeError, ValueError):
        return 0.0


def run(conn, min_net: int, batch_size: int, top_k: int, llm_model: str) -> int:
    """미분류 이벤트를 기존 토픽에 배정하거나 새 토픽으로 생성한다."""
    client = _get_client(llm_model)

    batch = events.fetch_unassigned(conn, min_net, batch_size)
    if not batch:
        print("[topic] 미배정 이벤트 없음")
        return 0

    print(f"[topic] 배치 시작: {len(batch)}건")
    processed = 0
    for ev in batch:
        try:
            print(f"[topic] event {ev.id} 처리 중: {ev.title}")

            cr = _call_json(
                client,
                build_topic_cause_result_prompt(ev.embedding_text),
                required_keys={"cause", "result"},
            )
            cause = str(cr["cause"]).strip()
            result = str(cr["result"]).strip()
            if not cause or not result:
                raise ValueError("LLM returned empty cause/result.")

            # 토픽 후보 검색은 cause 임베딩으로 먼저 좁히고, 최종 판단만 LLM에 맡긴다.
            cause_query_embedding = to_vector_literal(embed_query(cause))
            candidates = topic_causes.search_candidates(
                conn,
                cause_query_embedding,
                ev.category,
                DISTANCE_THRESHOLD,
                top_k,
            )
            if candidates:
                decision = _call_json(
                    client,
                    build_topic_assignment_prompt(
                        ev.title,
                        ev.summary,
                        cause,
                        result,
                        candidates,
                    ),
                    required_keys={"action"},
                )
            else:
                decision = {
                    "action": "create",
                    "new_title": ev.title,
                    "score": 0.0,
                    "reason": "검색 후보 없음",
                }

            # action 값·필수 키·topic_id 유효성 검증 (트랜잭션 진입 전)
            action = decision["action"]
            if action not in {"assign", "create"}:
                raise ValueError(f"Invalid topic action from LLM: {action!r}")
            if action == "assign" and (
                _load_decision_score(decision) < ASSIGN_SCORE_THRESHOLD
                or _reason_rejects_assignment(decision.get("reason"))
            ):
                decision = {
                    "action": "create",
                    "new_title": ev.title,
                    "score": _load_decision_score(decision),
                    "reason": (
                        "기존 후보와 직접 상관관계가 없다는 배정 사유가 감지되어 "
                        "새 토픽으로 생성"
                    ),
                }
                action = "create"

            result_embedding = to_vector_literal(embed_passage(result))
            cause_embedding = (
                to_vector_literal(embed_passage(cause)) if action == "create" else None
            )

            topic_update = None
            chosen = None
            if action == "assign":
                topic_id = int(decision["topic_id"])
                candidate_ids = {candidate.topic_id for candidate in candidates}
                if topic_id not in candidate_ids:
                    raise ValueError(f"LLM selected unknown topic_id={topic_id}.")
                chosen = next(candidate for candidate in candidates if candidate.topic_id == topic_id)
                topic_update = _call_json(
                    client,
                    build_topic_update_prompt(
                        chosen.title,
                        chosen.summary,
                        ev.title,
                        ev.summary,
                    ),
                    required_keys={"title", "summary"},
                )

            with conn.transaction():
                # 5-1. 토픽 확정
                if action == "create":
                    topic_id = topics.create_topic(
                        conn,
                        ev.category,
                        str(decision["new_title"]).strip(),
                        ev.summary,
                    )
                else:
                    topics.update_topic(
                        conn,
                        topic_id,
                        str(topic_update["title"]).strip(),
                        str(topic_update["summary"]).strip(),
                    )

                # 5-2. 이벤트 ↔ 토픽 매핑 (배정 근거 reason 함께 기록)
                events.assign_topic(conn, ev.id, topic_id, decision.get("reason"))
                topic_causes.add_cause(conn, topic_id, result, result_embedding)
                if action == "create":
                    # 새 토픽은 원인과 결과를 모두 저장해 다음 이벤트 검색 품질을 높인다.
                    topic_causes.add_cause(conn, topic_id, cause, cause_embedding)

                prev = events.find_prev_event(conn, topic_id, ev.id)
                if prev is not None:
                    prev_id, next_id = prev["id"], prev["next_event_id"]
                else:
                    prev_id, next_id = None, events.find_next_event_id(conn, topic_id, ev.id)
                events.link_into_chain(conn, ev.id, prev_id, next_id)

            processed += 1
            action_label = "create" if action == "create" else f"assign {topic_id}"
            print(f"[topic] event {ev.id} -> {action_label}")

        except Exception as exc:
            print(f"[topic] event {ev.id} failed: {exc}", file=sys.stderr)
            break

    return processed
