"""topic_causes 테이블 repository.

토픽 후보 검색(코사인 유사도)과 원인 데이터 누적 함수, SQL 상수를 제공한다.
"""

from dataclasses import dataclass, field
from collections import OrderedDict


@dataclass
class TopicCandidate:
    """토픽 후보 1건. 동일 topic_id에 매칭된 cause_text를 목록으로 묶는다."""
    topic_id: int
    title: str
    summary: str
    # 한 토픽이 여러 cause_text로 매칭될 수 있으므로 리스트로 수집한다.
    cause_texts: list = field(default_factory=list)


# ── SQL 상수 ────────────────────────────────────────────────────────────────

# 원인 임베딩 코사인 거리로 관련 토픽을 검색한다.
# 카테고리 구분 없이 전체 토픽을 대상으로 유사도 검색한다.
# 벡터 바인딩은 '[f1,f2,...]' 문자열 + ?::vector 캐스트 방식을 사용한다.
# (storage.py의 PostgresConnection이 register_vector를 호출하지 않으므로)
SEARCH_SQL = """
SELECT t.id    AS topic_id,
       t.title AS topic_title,
       t.summary AS topic_summary,
       tc.id   AS cause_id,
       tc.cause_text
FROM topic_causes tc
JOIN topics t ON t.id = tc.topic_id
WHERE tc.cause_embedding IS NOT NULL
ORDER BY tc.cause_embedding <=> ?::vector
LIMIT ?
"""

# 이벤트의 '결과(result)'를 topic_causes에 적재한다.
# 컬럼명은 cause_text이지만, 여기에는 이벤트 result를 저장한다.
# 이는 "해당 토픽에서 발생한 결과가 향후 유사 사건의 원인으로 작용한다"는
# 모델링 가정을 반영한 것이다(topic_classfication_logic.md §관련 테이블 참고).
INSERT_CAUSE_SQL = """
INSERT INTO topic_causes (topic_id, cause_text, cause_embedding)
VALUES (?, ?, ?::vector)
"""


# ── Repository 함수 ─────────────────────────────────────────────────────────

def search_candidates(
    conn,
    embedding_literal: str,
    top_k: int,
) -> list[TopicCandidate]:
    """원인 임베딩 코사인 거리로 토픽 후보 top-k를 검색한다.

    동일 topic_id가 여러 cause_text로 매칭될 수 있으므로
    토픽 단위로 그룹화해 반환한다(거리 오름차순 보존).

    embedding_literal: to_vector_literal()로 직렬화한 '[f1,f2,...]' 문자열.
    """
    rows = conn.query(SEARCH_SQL, (embedding_literal, top_k))

    # 거리 오름차순(쿼리 정렬 순서)을 유지하면서 topic_id 단위로 그룹화한다.
    seen: OrderedDict[int, TopicCandidate] = OrderedDict()
    for row in rows:
        tid = row["topic_id"]
        if tid not in seen:
            seen[tid] = TopicCandidate(
                topic_id=tid,
                title=row["topic_title"],
                summary=row["topic_summary"],
                cause_texts=[row["cause_text"]],
            )
        else:
            seen[tid].cause_texts.append(row["cause_text"])

    return list(seen.values())


def add_cause(conn, topic_id: int, cause_text: str, embedding_literal: str) -> None:
    """이벤트의 결과(result)를 topic_causes에 적재한다. 트랜잭션 내에서 호출한다.

    cause_text 파라미터에 이벤트의 'result'를 전달한다(D4 설계 결정 참고).
    embedding_literal: to_vector_literal()로 직렬화한 '[f1,f2,...]' 문자열.
    """
    conn.execute(INSERT_CAUSE_SQL, (topic_id, cause_text, embedding_literal))
