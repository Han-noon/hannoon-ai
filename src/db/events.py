"""events 테이블 repository.

토픽 배정에 필요한 이벤트 조회·갱신 함수와 SQL 상수를 제공한다.
이벤트 분류 로직과 공용으로 사용하는 모듈이므로, 이 파일에는
토픽 배정에 필요한 최소 함수만 정의한다.
"""

from dataclasses import dataclass


@dataclass
class Event:
    """토픽 배정 처리 단위. events 테이블의 최소 컬럼만 포함한다."""
    id: int
    category: str
    title: str
    summary: str
    # 대표 기사(제목 + 첫 문단). cause/result 추출 입력으로 사용한다.
    embedding_text: str


# ── SQL 상수 ────────────────────────────────────────────────────────────────

# 미배정 이벤트 조회: topic_id가 null이고, 실제 기사 수((article_count - abusing_count))가
# 임계값 이상인 이벤트를 (created_at, id) 기준 오름차순으로 가져온다.
# 순서를 보장해야 prev/next 체인이 시간순으로 연결된다.
FETCH_UNASSIGNED_SQL = """
SELECT id, category, title, summary, embedding_text
FROM events
WHERE topic_id IS NULL
  AND (article_count - abusing_count) >= ?
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


# ── Repository 함수 ─────────────────────────────────────────────────────────

def fetch_unassigned(conn, min_net: int, batch_size: int) -> list[Event]:
    """미배정 이벤트를 (created_at, id) 기준 오름차순으로 최대 batch_size건 조회한다.

    min_net: (article_count - abusing_count) 최소값. 기준 미달 이벤트는 제외한다.
    """
    rows = conn.query(FETCH_UNASSIGNED_SQL, (min_net, batch_size))
    return [Event(id=r["id"], category=r["category"], title=r["title"], summary=r["summary"],
                  embedding_text=r["embedding_text"])
            for r in rows]


def assign_topic(conn, event_id: int, topic_id: int, reason: str | None) -> None:
    """이벤트에 토픽을 배정하고 배정 근거(reason)를 기록한다. 트랜잭션 내에서 호출한다."""
    conn.execute(ASSIGN_TOPIC_SQL, (topic_id, reason, event_id))


def find_prev_event(conn, topic_id: int, event_id: int):
    """같은 토픽에서 (created_at, id) 기준 직전 노드를 반환한다.

    반환값: {"id": int, "next_event_id": int | None} 또는 None.
    트랜잭션 내에서 호출해야 assign_topic 결과가 반영된 상태를 읽는다.
    """
    return conn.query_one(FIND_PREV_EVENT_SQL, (topic_id, event_id))


def find_next_event_id(conn, topic_id: int, event_id: int) -> int | None:
    """같은 토픽에서 (created_at, id) 기준 직후 노드 id를 반환한다.

    엣지 케이스: 임계치 미달로 제외됐다 뒤늦게 배정된 이벤트가 기존 토픽의 가장 과거
    노드보다도 더 과거인 경우, find_prev_event() 결과가 None이 된다.
    이때 이 쿼리로 기존 head(직후 노드)를 찾아 새 이벤트를 체인 앞에 삽입한다.
    트랜잭션 내에서 호출한다.
    """
    row = conn.query_one(FIND_NEXT_EVENT_SQL, (topic_id, event_id))
    return row["id"] if row else None


def link_into_chain(conn, current_id: int, prev_id: int | None, next_id: int | None) -> None:
    """current를 prev↔current↔next 위치에 doubly-linked list 삽입한다.

    - current.prev = prev_id / current.next = next_id
    - prev 있으면 prev.next = current_id
    - next 있으면 next.prev = current_id

    호출 측 트랜잭션 내에서 원자적으로 실행한다.
    """
    conn.execute(UPDATE_CHAIN_SQL, (prev_id, next_id, current_id))
    if prev_id is not None:
        conn.execute(UPDATE_NEXT_EVENT_SQL, (current_id, prev_id))
    if next_id is not None:
        conn.execute(UPDATE_PREV_EVENT_SQL, (current_id, next_id))


# ── (이벤트 분류기 파이프라인 전용 확장) ──────────────────────────────────

# 기사 시간 기준 2일 이내의 후보 이벤트를 pgvector 거리 순으로 검색
SEARCH_CANDIDATE_EVENTS_SQL = """
SELECT id, title, core_content, summary,
       (embedding <=> ?::vector) as distance
FROM events
WHERE (updated_at + INTERVAL '2 day' >= ?)
  AND (embedding <=> ?::vector) <= ?
ORDER BY distance ASC
LIMIT ?
"""

# 기존 이벤트 업데이트
UPDATE_EVENT_DEADLINE_SQL = """
UPDATE events 
SET updated_at = ?, 
    article_count = article_count + 1 
WHERE id = ?
"""

# 신규 이벤트 생성 (RETURNING id로 방금 생성된 id 획득)
INSERT_NEW_EVENT_SQL = """
INSERT INTO events (
    topic_id, category, title, summary, 
    core_content, embedding_text, embedding, article_count
) VALUES (NULL, ?::category, ?, ?, ?, ?, ?::vector, 1)
RETURNING id
"""

# 기사-이벤트 관계 테이블 맵핑 삽입
INSERT_EVENT_ARTICLE_MAP_SQL = """
INSERT INTO event_articles (event_id, article_id, reason)
VALUES (?, ?, ?)
"""

def search_candidate_events(conn, embedding: list[float], published_at_str: str, max_distance: float, top_k: int):
    """유사한 후보 이벤트 목록을 검색합니다."""
    return conn.query(SEARCH_CANDIDATE_EVENTS_SQL, (embedding, published_at_str, embedding, max_distance, top_k))

def update_event_deadline(conn, event_id: int, published_at_str: str):
    """기존 이벤트의 데드라인 시간과 카운트를 갱신합니다."""
    conn.execute(UPDATE_EVENT_DEADLINE_SQL, (published_at_str, event_id))

def create_new_event(conn, category: str, title: str, summary: str, core_content: str, embedding_text: str, embedding: list[float]) -> int:
    """새로운 이벤트를 개설하고 생성된 ID를 반환합니다."""
    row = conn.query_one(INSERT_NEW_EVENT_SQL, (category, title, summary, core_content, embedding_text, embedding))
    return row["id"]

def link_article_to_event(conn, event_id: int, article_id: int, reason: str):
    """기사와 이벤트를 맵핑 테이블에 연결합니다."""
    conn.execute(INSERT_EVENT_ARTICLE_MAP_SQL, (event_id, article_id, reason))