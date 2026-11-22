from __future__ import annotations

from dataclasses import dataclass

from event_classifier.settings import (
    ENTITY_CANDIDATE_LIMIT,
    ENTITY_DF_CUTOFF_DAYS,
    ENTITY_DF_CUTOFF_EVENTS,
    ENTITY_DISTANCE_CEILING,
    K_VEC,
    MIN_SHARED_ENTITIES,
)
from summary_utils import normalize_summary

# 결합 점수(distance와 entity 자카드를 합친 내부 랭킹용 점수, 값이 낮을수록 좋음) 가중치.
# 실측 후 조정 가능한 placeholder(Implementation Step 5, 2026-07-22 확정).
W_DISTANCE = 0.7
W_ENTITY = 0.3


@dataclass
class Event:
    """토픽 분류 단계에서 사용하는 이벤트 행."""

    id: int
    category: str
    title: str
    summary: str
    # 대표 기사 제목 + 전체 AI요약(자연문). cause/result 추출 입력으로 사용한다.
    embedding_text: str


# ── SQL 상수 ────────────────────────────────────────────────────────────────

# 미배정 이벤트 조회: topic_id가 null이고, 기사 수가 임계값 이상인 이벤트를
# (created_at, id) 기준 오름차순으로 가져온다.
# 순서를 보장해야 prev/next 체인이 시간순으로 연결된다.
FETCH_UNASSIGNED_SQL = """
SELECT id, category, title, summary, embedding_text
FROM events
WHERE topic_id IS NULL
  AND article_count >= ?
ORDER BY created_at ASC, id ASC
LIMIT ?
"""

# 이벤트에 토픽을 배정하고 배정 근거(reason)를 기록한다.
ASSIGN_TOPIC_SQL = "UPDATE events SET topic_id = ?, reason = ? WHERE id = ?"

# 같은 토픽에서 (created_at, id) 기준 직전 노드와 그 노드의 기존 next를 조회한다.
# next 컬럼을 함께 가져오므로 중간 삽입 시 별도 next 조회 없이 처리할 수 있다.
# (created_at, id) < 현재 이벤트: < 비교가 자기 자신을 자동 제외한다.
FIND_PREV_EVENT_SQL = """
SELECT id, next_event_id
FROM events
WHERE topic_id = ?
  AND (created_at, id) < (SELECT created_at, id FROM events WHERE id = ?)
ORDER BY created_at DESC, id DESC
LIMIT 1
"""

# (created_at, id) 기준 직후 노드를 조회한다.
# 엣지 케이스: 임계치 미달로 제외됐다 뒤늦게 배정된 이벤트가 기존 토픽의 가장 과거
# 노드보다도 더 과거인 경우, FIND_PREV_EVENT_SQL 결과가 None이 된다.
# 이때 이 쿼리로 기존 head(직후 노드)를 찾아 새 이벤트를 체인 앞에 삽입한다.
FIND_NEXT_EVENT_SQL = """
SELECT id
FROM events
WHERE topic_id = ?
  AND (created_at, id) > (SELECT created_at, id FROM events WHERE id = ?)
ORDER BY created_at ASC, id ASC
LIMIT 1
"""

# 현재 이벤트의 prev/next를 동시에 설정한다 (params: prev_id, next_id, current_id).
UPDATE_CHAIN_SQL = "UPDATE events SET prev_event_id = ?, next_event_id = ? WHERE id = ?"

# 직전 이벤트의 next_event_id를 설정한다 (params: new_next_id, target_id).
UPDATE_NEXT_EVENT_SQL = "UPDATE events SET next_event_id = ? WHERE id = ?"

# 직후 이벤트의 prev_event_id를 설정한다 (params: new_prev_id, target_id).
UPDATE_PREV_EVENT_SQL = "UPDATE events SET prev_event_id = ? WHERE id = ?"

# 결정점 0/2(2026-07-22 확정, .omc/plans/event-classification-ner-redesign-plan.md):
# 시간창(2일)과 DISTANCE_THRESHOLD 단일 게이트를 폐지하고, 앱 레벨 2-fetch로 대체한다.
#   (a) 벡터 fetch: event_articles를 join하지 않은 단독 top-K_vec ANN(hnsw 활용 보장).
#   (b) 엔티티 fetch: article_entities 역색인 + 4중 하드 필터(최소 공유 수, df컷, LIMIT M,
#       느슨한 거리 상한)로 벡터 게이트 없이 후보를 회수.
#   (c) 앱단 병합: 두 결과를 article_id 기준으로 합쳐 기사 멤버 단위 결합 점수를 계산하고
#       event_id로 집계, 최선 멤버를 이벤트 대표값으로 채택한다(멤버 단위 원칙 유지).
SEARCH_VECTOR_CANDIDATES_SQL = """
SELECT a.id AS article_id, a.embedding <=> ?::vector AS distance
FROM articles a
WHERE a.embedding IS NOT NULL
ORDER BY a.embedding <=> ?::vector
LIMIT ?
"""

# hnsw 색인 스캔은 탐색 폭(hnsw.ef_search, 기본 40)보다 많은 행을 돌려주지 않는다.
# 기본값이면 LIMIT K_VEC(100)을 요청해도 약 40행만 나오므로(기사가 쌓여 색인을 쓰는 규모에서만 발생),
# 벡터 fetch 직전에 탐색 폭을 K_VEC의 2배(기본값 40 이상, pgvector 상한 1000)로 올린다. 2배는 근사 검색이
# 진짜 상위 K를 놓치지 않게 두는 여유(313건 실측: 100은 가끔 1개 누락, 150 이상 전부 일치). 연결이 autocommit이라
# SET LOCAL은 효과가 없어 세션 단위로 설정한다.
HNSW_EF_SEARCH = max(40, min(1000, 2 * K_VEC))
SET_HNSW_EF_SEARCH_SQL = "SELECT set_config('hnsw.ef_search', ?, false)"

# 새 기사 entities 중 "너무 흔한" 엔티티를 찾아 공유 엔티티 집합에서 제외한다(df 컷).
# 최근 N일 내 서로 다른 event_id에 20회 이상 등장하면 흔한 엔티티로 간주(모두 placeholder).
ENTITY_DF_CUTOFF_SQL = """
SELECT ae.entity
FROM article_entities ae
JOIN event_articles ea ON ea.article_id = ae.article_id
JOIN events e ON e.id = ea.event_id
WHERE ae.entity = ANY(?)
  AND e.created_at >= now() - make_interval(days => ?)
GROUP BY ae.entity
HAVING COUNT(DISTINCT ea.event_id) >= ?
"""

# df 컷을 통과한(흔하지 않은) entities와 최소 MIN_SHARED_ENTITIES개 이상 겹치는 기사를
# 공유 엔티티 수 내림차순으로 최대 M건 회수한다(무한 성장 방지 캡 (i)(ii)(iii)).
ENTITY_CANDIDATE_SQL = """
SELECT article_id, COUNT(DISTINCT entity) AS shared_count
FROM article_entities
WHERE entity = ANY(?)
GROUP BY article_id
HAVING COUNT(DISTINCT entity) >= ?
ORDER BY shared_count DESC
LIMIT ?
"""

# 엔티티 경로로 회수된 후보만 대상으로 임베딩 거리를 계산한다(전체 스캔이 아니라
# 소수 대상 특정 쿼리). 여기서 계산한 거리가 ENTITY_DISTANCE_CEILING을 넘으면
# 하드 필터로 완전히 제외한다(캡 (iv), 스코어링 가중치 아님).
ENTITY_CANDIDATE_DISTANCE_SQL = """
SELECT id AS article_id, embedding <=> ?::vector AS distance
FROM articles
WHERE id = ANY(?) AND embedding IS NOT NULL
"""

# (a)/(b)에서 살아남은 article_id를 event_articles에 join해 실제 이벤트 멤버만 남기고
# (이 join이 벡터 fetch에서 분리되어 앱단에서 일어나는 지점), earliest_published_at
# 계산에 쓸 published_at과 판정 근거인 멤버 사건 내용도 함께 가져온다.
EVENT_MEMBER_LOOKUP_SQL = """
SELECT ea.article_id, ea.event_id, a.published_at, a.core_content
FROM event_articles ea
JOIN articles a ON a.id = ea.article_id
WHERE ea.article_id = ANY(?)
"""

EVENT_ROWS_SQL = """
SELECT id, category, title, core_content, summary, article_count
FROM events
WHERE id = ANY(?)
"""

UPDATE_EVENT_SUMMARY_SQL = """
UPDATE events
SET summary = ?,
    updated_at = now()
WHERE id = ?
"""

# 신규 이벤트 생성 (RETURNING id로 방금 생성된 id 획득)
INSERT_NEW_EVENT_SQL = """
INSERT INTO events (
    topic_id, category, title, summary,
    core_content, embedding_text, embedding, article_count, event_image_url
) VALUES (NULL, ?::category, ?, ?, ?, ?, ?::vector, 0, ?)
RETURNING id
"""

INSERT_EVENT_ARTICLE_MAP_SQL = """
INSERT INTO event_articles (event_id, article_id, reason)
VALUES (?, ?, ?)
"""


def fetch_unassigned(conn, min_net: int, batch_size: int) -> list[Event]:
    """토픽이 아직 없는 이벤트 중 정상 기사 수가 기준 이상인 항목을 가져온다."""
    rows = conn.query(FETCH_UNASSIGNED_SQL, (min_net, batch_size))
    return [
        Event(
            id=row["id"],
            category=row["category"],
            title=row["title"],
            summary=row["summary"],
            embedding_text=row["embedding_text"],
        )
        for row in rows
    ]


def assign_topic(conn, event_id: int, topic_id: int, reason: str | None) -> None:
    """이벤트를 토픽에 배정한다."""
    conn.execute(ASSIGN_TOPIC_SQL, (topic_id, reason, event_id))


def find_prev_event(conn, topic_id: int, event_id: int):
    """같은 토픽에서 현재 이벤트보다 앞선 이벤트를 찾는다."""
    return conn.query_one(FIND_PREV_EVENT_SQL, (topic_id, event_id))


def find_next_event_id(conn, topic_id: int, event_id: int) -> int | None:
    """같은 토픽에서 현재 이벤트보다 뒤에 오는 이벤트 id를 찾는다."""
    row = conn.query_one(FIND_NEXT_EVENT_SQL, (topic_id, event_id))
    return row["id"] if row else None


def link_into_chain(conn, current_id: int, prev_id: int | None, next_id: int | None) -> None:
    """토픽 내부 이벤트 연결 리스트(prev/next)를 갱신한다."""
    conn.execute(UPDATE_CHAIN_SQL, (prev_id, next_id, current_id))
    if prev_id is not None:
        conn.execute(UPDATE_NEXT_EVENT_SQL, (current_id, prev_id))
    if next_id is not None:
        conn.execute(UPDATE_PREV_EVENT_SQL, (current_id, next_id))


def search_candidate_events(
    conn,
    embedding_literal: str,
    entities: list[str] | None,
    top_k: int,
) -> list[dict]:
    """기사 임베딩(top-K_vec ANN)과 entities(역색인 자카드, 4중 하드 필터)로 후보 이벤트를
    찾아 앱 레벨에서 병합한다(결정점 0/2, 2026-07-22 확정).

    반환 이벤트마다 어느 article_id가 대표 결합 점수를 만들었는지 source_article_id로
    로깅한다(멤버 단위 원칙 추적 가능성 확보).
    """
    conn.query(SET_HNSW_EF_SEARCH_SQL, (str(HNSW_EF_SEARCH),))
    vector_rows = conn.query(
        SEARCH_VECTOR_CANDIDATES_SQL,
        (embedding_literal, embedding_literal, K_VEC),
    )
    vector_by_article: dict[int, float] = {
        row["article_id"]: float(row["distance"]) for row in vector_rows
    }

    # entity_by_article: article_id -> (embedding distance, shared entity count)
    entity_by_article: dict[int, tuple[float, int]] = {}
    clean_entities = [entity for entity in (entities or []) if entity]
    if len(clean_entities) >= MIN_SHARED_ENTITIES:
        df_rows = conn.query(
            ENTITY_DF_CUTOFF_SQL,
            (clean_entities, ENTITY_DF_CUTOFF_DAYS, ENTITY_DF_CUTOFF_EVENTS),
        )
        excluded_entities = {row["entity"] for row in df_rows}
        allowed_entities = [e for e in clean_entities if e not in excluded_entities]

        if len(allowed_entities) >= MIN_SHARED_ENTITIES:
            candidate_rows = conn.query(
                ENTITY_CANDIDATE_SQL,
                (allowed_entities, MIN_SHARED_ENTITIES, ENTITY_CANDIDATE_LIMIT),
            )
            shared_counts = {row["article_id"]: row["shared_count"] for row in candidate_rows}
            candidate_ids = list(shared_counts.keys())

            if candidate_ids:
                distance_rows = conn.query(
                    ENTITY_CANDIDATE_DISTANCE_SQL,
                    (embedding_literal, candidate_ids),
                )
                for row in distance_rows:
                    distance = float(row["distance"])
                    if distance <= ENTITY_DISTANCE_CEILING:
                        article_id = row["article_id"]
                        entity_by_article[article_id] = (distance, shared_counts[article_id])

    all_article_ids = list(set(vector_by_article) | set(entity_by_article))
    if not all_article_ids:
        return []

    member_rows = conn.query(EVENT_MEMBER_LOOKUP_SQL, (all_article_ids,))
    if not member_rows:
        return []

    entities_count = len(clean_entities) or 1  # 0 분모 방지(entities 없는 기사)

    # article_id 단위 결합 점수(distance·entity 겹침 가중 합, 낮을수록 좋음)를 계산해
    # event_id별 최선 멤버를 대표값으로 채택한다(멤버 단위 원칙, centroid 재도입 아님).
    best_by_event: dict[int, dict] = {}
    earliest_by_event: dict[int, str] = {}
    for row in member_rows:
        article_id = row["article_id"]
        event_id = row["event_id"]
        published_at = row["published_at"]

        if published_at is not None:
            current_earliest = earliest_by_event.get(event_id)
            if current_earliest is None or published_at < current_earliest:
                earliest_by_event[event_id] = published_at

        distance = vector_by_article.get(article_id)
        shared_count = 0
        if article_id in entity_by_article:
            entity_distance, shared_count = entity_by_article[article_id]
            if distance is None:
                distance = entity_distance

        if distance is None:
            continue  # 이론상 발생하지 않음(둘 중 한 fetch 경로에서 왔어야 함)

        entity_score = shared_count / entities_count
        combined_score = W_DISTANCE * distance + W_ENTITY * (1 - entity_score)

        best = best_by_event.get(event_id)
        if best is None or combined_score < best["combined_score"]:
            best_by_event[event_id] = {
                "article_id": article_id,
                "distance": distance,
                "combined_score": combined_score,
                "published_at": published_at,
                "core_content": row["core_content"],
            }

    if not best_by_event:
        return []

    event_rows = conn.query(EVENT_ROWS_SQL, (list(best_by_event.keys()),))
    event_by_id = {row["id"]: row for row in event_rows}

    results = []
    for event_id, best in best_by_event.items():
        event = event_by_id.get(event_id)
        if event is None:
            continue
        results.append(
            {
                "id": event["id"],
                "category": event["category"],
                "title": event["title"],
                "core_content": event["core_content"],
                "summary": event["summary"],
                "article_count": event["article_count"],
                "distance": best["distance"],
                "earliest_published_at": earliest_by_event.get(event_id),
                "source_article_id": best["article_id"],
                "source_published_at": best["published_at"],
                "source_core_content": best["core_content"],
            }
        )

    results.sort(key=lambda r: best_by_event[r["id"]]["combined_score"])
    return results[:top_k]


def create_new_event(
    conn,
    category: str,
    title: str,
    summary: str,
    core_content: str,
    embedding_text: str,
    embedding_literal: str,
    event_image_url: str | None = None,
) -> int:
    """새 이벤트를 만들고 생성된 id를 반환한다."""
    summary = normalize_summary(summary) or normalize_summary(title)
    row = conn.query_one(
        INSERT_NEW_EVENT_SQL,
        (
            category,
            title,
            summary,
            core_content,
            embedding_text,
            embedding_literal,
            event_image_url,
        ),
    )
    return row["id"]


def update_event_summary(conn, event_id: int, summary: str) -> None:
    """이벤트 요약을 공통 길이 규칙에 맞춰 갱신한다."""
    summary = normalize_summary(summary)
    if not summary:
        raise ValueError("Event summary is empty.")
    conn.execute(UPDATE_EVENT_SUMMARY_SQL, (summary, event_id))


def link_article_to_event(conn, event_id: int, article_id: int, reason: str):
    """기사와 이벤트를 관계 테이블에 연결한다."""
    conn.execute(INSERT_EVENT_ARTICLE_MAP_SQL, (event_id, article_id, reason))
