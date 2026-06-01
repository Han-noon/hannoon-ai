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


# ── SQL 상수 ────────────────────────────────────────────────────────────────

# 미배정 이벤트 조회: topic_id가 null이고, 실제 기사 수((article_count - abusing_count))가
# 임계값 이상인 이벤트를 created_at 오름차순으로 가져온다.
# 순서를 보장해야 prev/next 체인이 시간순으로 연결된다.
FETCH_UNASSIGNED_SQL = """
SELECT id, category, title, summary
FROM events
WHERE topic_id IS NULL
  AND (article_count - abusing_count) >= ?
ORDER BY created_at ASC
LIMIT ?
"""

# 이벤트에 토픽을 배정하고 배정 근거(reason)를 기록한다.
ASSIGN_TOPIC_SQL = "UPDATE events SET topic_id = ?, reason = ? WHERE id = ?"

# 같은 토픽에서 가장 최근에 배정된 이벤트를 조회한다.
# 새 이벤트는 항상 해당 토픽의 마지막 노드 뒤에 연결되므로
# 시각 기준 필터 없이 단순 내림차순 정렬로 직전 노드를 찾는다.
FIND_PREV_EVENT_SQL = """
SELECT id
FROM events
WHERE topic_id = ?
  AND id <> ?
ORDER BY created_at DESC
LIMIT 1
"""

# 현재 이벤트의 prev_event_id를 설정한다 (params: prev_id, current_id).
UPDATE_PREV_EVENT_SQL = "UPDATE events SET prev_event_id = ? WHERE id = ?"

# 직전 이벤트의 next_event_id를 설정한다 (params: current_id, prev_id).
UPDATE_NEXT_EVENT_SQL = "UPDATE events SET next_event_id = ? WHERE id = ?"


# ── Repository 함수 ─────────────────────────────────────────────────────────

def fetch_unassigned(conn, min_net: int, batch_size: int) -> list[Event]:
    """미배정 이벤트를 created_at ASC 순으로 최대 batch_size건 조회한다.

    min_net: (article_count - abusing_count) 최소값. 기준 미달 이벤트는 제외한다.
    """
    rows = conn.query(FETCH_UNASSIGNED_SQL, (min_net, batch_size))
    return [Event(id=r["id"], category=r["category"], title=r["title"], summary=r["summary"])
            for r in rows]


def assign_topic(conn, event_id: int, topic_id: int, reason: str | None) -> None:
    """이벤트에 토픽을 배정하고 배정 근거(reason)를 기록한다. 트랜잭션 내에서 호출한다."""
    conn.execute(ASSIGN_TOPIC_SQL, (topic_id, reason, event_id))


def find_prev_event_id(conn, topic_id: int, event_id: int) -> int | None:
    """같은 토픽에서 현재 이벤트 직전(created_at 기준)에 배정된 이벤트 id를 반환한다.

    없으면 None (현재 이벤트가 해당 토픽의 첫 번째 노드).
    트랜잭션 내에서 호출해야 assign_topic 결과가 반영된 상태를 읽는다.
    """
    row = conn.query_one(FIND_PREV_EVENT_SQL, (topic_id, event_id))
    return row["id"] if row else None


def link_chain(conn, prev_id: int, current_id: int) -> None:
    """직전 이벤트(prev_id)와 현재 이벤트(current_id)를 양방향으로 연결한다.

    - current.prev_event_id = prev_id
    - prev.next_event_id   = current_id

    두 UPDATE를 호출 측 트랜잭션 내에서 원자적으로 실행하기 위해
    이 함수 자체는 트랜잭션을 열지 않는다.
    """
    conn.execute(UPDATE_PREV_EVENT_SQL, (prev_id, current_id))
    conn.execute(UPDATE_NEXT_EVENT_SQL, (current_id, prev_id))
