"""topics 테이블 repository.

토픽 생성 함수와 SQL 상수를 제공한다.
"""

from dataclasses import dataclass


@dataclass
class Topic:
    """topics 테이블 행을 표현하는 dataclass."""
    id: int
    category: str
    title: str
    summary: str


# ── SQL 상수 ────────────────────────────────────────────────────────────────

# 새 토픽을 삽입하고 생성된 id를 반환한다.
# category는 public.category enum 타입이므로 ?::category 캐스트가 필요하다.
# PostgresCursor의 자동 RETURNING id 처리는 'INSERT INTO ARTICLES'에만 적용되므로,
# topics INSERT는 명시적으로 RETURNING id를 붙이고 query_one으로 id를 받는다.
INSERT_TOPIC_SQL = """
INSERT INTO topics (category, title, summary)
VALUES (?::category, ?, ?)
RETURNING id
"""


# ── Repository 함수 ─────────────────────────────────────────────────────────

def create_topic(conn, category: str, title: str, summary: str) -> int:
    """새 토픽을 생성하고 생성된 id를 반환한다. 트랜잭션 내에서 호출한다."""
    row = conn.query_one(INSERT_TOPIC_SQL, (category, title, summary))
    return row["id"]
