"""articles 테이블 repository."""

# status가 'ready'인 기사들을 가져옵니다.
FETCH_READY_ARTICLES_SQL = """
SELECT id, content, published_at, category
FROM articles
WHERE status = 'ready'
ORDER BY published_at ASC, id ASC
LIMIT ?
"""

# 기사의 embedding, core_content, status를 일괄 업데이트합니다.
UPDATE_ARTICLE_ANALYSIS_SQL = """
UPDATE articles 
SET embedding = ?::vector, 
    core_content = ?,
    status = 'processed',
    updated_at = now()
WHERE id = ?
"""

def fetch_ready_articles(conn, limit: int) -> list[dict]:
    """처리가 필요한 ready 상태의 기사들을 조회합니다."""
    return conn.query(FETCH_READY_ARTICLES_SQL, (limit,))

def update_article_analysis(conn, article_id: int, embedding: list[float], core_content: str) -> None:
    """기사에 추출된 본문 특징과 임베딩을 업데이트합니다. 트랜잭션 내에서 실행됩니다."""
    conn.execute(UPDATE_ARTICLE_ANALYSIS_SQL, (embedding, core_content, article_id))