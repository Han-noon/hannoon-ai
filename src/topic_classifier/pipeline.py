"""토픽 분류 파이프라인.

미배정 이벤트를 배치로 읽어 LLM + 임베딩 유사도 검색으로 토픽에 배정하거나
새 토픽을 생성한 뒤, topic_causes 누적과 이벤트 체인(prev/next) 연결까지
단일 트랜잭션으로 처리한다.
"""

import json
import sys

from db import events, topics, topic_causes
from embedding import embed, to_vector_literal
from openai_client.client import OpenAIClient
from topic_classifier.prompts import (
    build_topic_cause_result_prompt,
    build_topic_assignment_prompt,
    build_topic_update_prompt,
)
from topic_classifier.settings import LLM_MODEL

# LLM 클라이언트는 첫 호출 시 1회 생성되는 싱글톤이다.
# run()이 시작 시 CLI 모델로 초기화하며, 미지정 시 settings.LLM_MODEL을 쓴다.
_client: OpenAIClient | None = None


def _get_client(model: str = LLM_MODEL) -> OpenAIClient:
    global _client
    if _client is None:
        _client = OpenAIClient(model=model)
    return _client


# ── 스키마 검증 ──────────────────────────────────────────────────────────────

def _validate_topic_schema(conn) -> None:
    """토픽 분류에 필요한 테이블과 컬럼이 DB에 준비됐는지 검증한다.

    collector의 _validate_postgres_schema와 마찬가지로, 앱은 DDL을 실행하지 않고
    Supabase 마이그레이션이 선행 적용됐는지만 확인한다.
    """
    required = {
        "events": {
            "id", "topic_id", "category", "title", "summary", "embedding_text",
            "article_count", "abusing_count", "created_at",
            "prev_event_id", "next_event_id", "reason",
        },
        "topics": {"id", "category", "title", "summary"},
        "topic_causes": {"id", "topic_id", "cause_text", "cause_embedding"},
    }

    table_names = tuple(required)
    placeholders = ", ".join("?" for _ in table_names)
    existing_tables = {
        row["table_name"]
        for row in conn.query(
            f"SELECT table_name FROM information_schema.tables "
            f"WHERE table_schema = 'public' AND table_name IN ({placeholders})",
            table_names,
        )
    }
    missing = sorted(set(table_names) - existing_tables)
    if missing:
        raise RuntimeError(
            "토픽 분류에 필요한 테이블이 없습니다: "
            + ", ".join(missing)
            + ". Supabase 마이그레이션을 먼저 적용하세요."
        )

    for table, cols in required.items():
        existing_cols = {
            row["column_name"]
            for row in conn.query(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = ?",
                (table,),
            )
        }
        missing_cols = sorted(cols - existing_cols)
        if missing_cols:
            raise RuntimeError(
                f"'{table}' 테이블에 필요한 컬럼이 없습니다: "
                + ", ".join(missing_cols)
                + ". Supabase 마이그레이션을 먼저 적용하세요."
            )


# ── LLM 호출 헬퍼 ────────────────────────────────────────────────────────────

def _call_json(prompt: str, required_keys: set[str]) -> dict:
    """LLM에 JSON 응답을 요청하고, 필수 키가 모두 있는지 검증한다.

    required_keys에 없는 키가 응답에 있어도 허용한다.
    필수 키가 빠져 있으면 ValueError를 발생시킨다.
    """
    client = _get_client()
    raw = client.request(
        prompt,
        temperature=0,
        response_format={"type": "json_object"},
    )
    data = json.loads(raw)
    missing = required_keys - set(data)
    if missing:
        raise ValueError(f"LLM 응답에 필수 키가 없습니다: {missing}. 응답: {raw}")
    return data


# ── 파이프라인 ────────────────────────────────────────────────────────────────

def run(conn, min_net: int, batch_size: int, top_k: int, llm_model: str) -> int:
    """미배정 이벤트 배치를 처리하고 처리된 이벤트 수를 반환한다.

    처리 순서(created_at ASC)를 반드시 지킨다.
    후행 이벤트가 선행보다 먼저 배정되면 prev/next 체인이 깨지기 때문이다.

    한 이벤트에서 오류가 발생하면 이후 이벤트를 건너뛰지 않고 배치를 중단한다.
    미처리 이벤트는 topic_id IS NULL로 남아 다음 실행에서 같은 순서로 재시도된다.

    min_net: 실제 기사 수((article_count - abusing_count)) 최소값.
    batch_size: 한 번에 처리할 최대 이벤트 수.
    top_k: 토픽 후보 검색 수.
    llm_model: cause/result 추출·배정·최신화에 사용할 LLM 모델.
    """
    _get_client(llm_model)
    _validate_topic_schema(conn)

    batch = events.fetch_unassigned(conn, min_net, batch_size)
    if not batch:
        print("[topic] 미배정 이벤트 없음")
        return 0

    print(f"[topic] 배치 시작: {len(batch)}건")
    processed = 0
    for ev in batch:
        try:
            print(f"[topic] event {ev.id} 처리 중: {ev.title}")

            # 2단계: 대표 기사(embedding_text)에서 원인(cause)과 결과(result) 추출
            print(f"[topic] event {ev.id} → cause/result 추출 중")
            cr = _call_json(
                build_topic_cause_result_prompt(ev.embedding_text),
                required_keys={"cause", "result"},
            )
            print(f"[topic] event {ev.id}  cause: {cr['cause']}")
            print(f"[topic] event {ev.id}  result: {cr['result']}")

            # 3단계: 원인 임베딩으로 토픽 후보 검색
            print(f"[topic] event {ev.id} → 임베딩·후보 검색 중")
            cause_lit = to_vector_literal(embed(cr["cause"]))
            candidates = topic_causes.search_candidates(conn, cause_lit, top_k)
            print(f"[topic] event {ev.id}  후보 {len(candidates)}건")

            # 4단계: LLM으로 토픽 배정 여부 결정
            print(f"[topic] event {ev.id} → 배정 결정 중")
            decision = _call_json(
                build_topic_assignment_prompt(ev.title, ev.summary, candidates),
                required_keys={"action"},
            )
            print(f"[topic] event {ev.id}  action: {decision['action']}"
                  + (f"  reason: {decision['reason']}" if decision.get("reason") else ""))

            # action 값·필수 키·topic_id 유효성 검증 (트랜잭션 진입 전)
            action = decision["action"]
            if action not in {"assign", "create"}:
                raise ValueError(f"LLM이 알 수 없는 action을 반환했습니다: {action!r}")
            if action == "create":
                if "new_title" not in decision:
                    raise ValueError("action=create인데 new_title이 없습니다.")
            else:
                if "topic_id" not in decision:
                    raise ValueError("action=assign인데 topic_id가 없습니다.")
                candidate_ids = {c.topic_id for c in candidates}
                if int(decision["topic_id"]) not in candidate_ids:
                    raise ValueError(
                        f"LLM이 후보에 없는 topic_id={decision['topic_id']}를 반환했습니다."
                    )

            # assign이면 배정 시점 기준으로 토픽 제목·요약을 최신화한다(LLM, 트랜잭션 밖).
            # 현재 토픽 제목·요약은 후보 검색 결과에 이미 들어 있어 추가 조회가 필요 없다.
            topic_update = None
            if action == "assign":
                chosen = next(
                    c for c in candidates if c.topic_id == int(decision["topic_id"])
                )
                topic_update = _call_json(
                    build_topic_update_prompt(
                        chosen.title, chosen.summary, ev.title, ev.summary
                    ),
                    required_keys={"title", "summary"},
                )

            # result 임베딩은 트랜잭션 밖에서 미리 계산해 점유 시간을 줄인다.
            result_lit = to_vector_literal(embed(cr["result"]))

            # 5단계: 토픽 확정·매핑·cause 누적·체인 연결을 단일 트랜잭션으로
            with conn.transaction():
                # 5-1. 토픽 확정
                if action == "create":
                    # title만 LLM이 생성한 값 사용, category·summary는 이벤트 값 그대로
                    topic_id = topics.create_topic(
                        conn, ev.category, decision["new_title"], ev.summary
                    )
                else:
                    topic_id = int(decision["topic_id"])
                    # 5-1b. 기존 토픽 제목·요약을 최신 이벤트 기준으로 갱신
                    topics.update_topic(
                        conn, topic_id, topic_update["title"], topic_update["summary"]
                    )

                # 5-2. 이벤트 ↔ 토픽 매핑 (배정 근거 reason 함께 기록)
                events.assign_topic(conn, ev.id, topic_id, decision.get("reason"))

                # 5-3. topic_causes 누적 (result를 cause_text로 저장한다, D4)
                topic_causes.add_cause(conn, topic_id, cr["result"], result_lit)
                # 신규 토픽은 최초 이벤트의 cause가 발단이 되므로, 이후 유사 이벤트가
                # 검색으로 이 토픽을 찾을 수 있도록 cause도 함께 적재한다.
                if action == "create":
                    topic_causes.add_cause(conn, topic_id, cr["cause"], cause_lit)

                # 5-4. 이벤트 체인 연결 (동일 토픽 내 시간순, linked-list 삽입)
                prev = events.find_prev_event(conn, topic_id, ev.id)
                if prev is not None:
                    prev_id, next_id = prev["id"], prev["next_event_id"]
                else:
                    prev_id, next_id = None, events.find_next_event_id(conn, topic_id, ev.id)
                events.link_into_chain(conn, ev.id, prev_id, next_id)

            processed += 1
            action_label = "create" if decision["action"] == "create" else f"assign → {topic_id}"
            print(f"[topic] event {ev.id} → {action_label} ✓")
            if action == "assign":
                print(f"[topic] title: {chosen.title!r} → {topic_update['title']!r}")
                print(f"[topic] summary: {chosen.summary!r} → {topic_update['summary']!r}")
            print()

        except Exception as e:
            # 순서 보장을 위해 배치를 중단한다(D2).
            # 실패한 이벤트는 topic_id IS NULL로 남아 다음 실행에서 재시도된다.
            print(f"[topic] event {ev.id} 처리 실패: {e}", file=sys.stderr)
            break

    return processed
