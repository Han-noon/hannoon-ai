import os
import sqlite3
from datetime import datetime

try:
    import psycopg
except Exception:  # pragma: no cover - optional dependency for local SQLite use
    psycopg = None

try:
    from dateutil import parser as date_parser
except Exception:  # pragma: no cover - optional dependency
    date_parser = None


class PostgresCursor:
    """SQLite와 비슷하게 사용할 수 있도록 psycopg 커서를 감싸는 래퍼.

    기존 수집 코드는 SQLite의 `?` placeholder와 `lastrowid`에 맞춰 작성되어 있다.
    Postgres에서는 placeholder가 `%s`이고 INSERT 후 생성 ID를 `RETURNING id`로
    받아야 하므로, 이 클래스가 그 차이를 흡수한다.
    """

    def __init__(self, cursor):
        self._cursor = cursor
        self.lastrowid: int | None = None

    def execute(self, sql: str, params: tuple | list | None = None):
        """SQLite 스타일 SQL을 Postgres 스타일로 변환해 실행한다."""
        sql = _to_postgres_sql(sql)
        if _is_article_insert(sql):
            sql = f"{sql.rstrip()} RETURNING id"
            self._cursor.execute(sql, params or ())
            row = self._cursor.fetchone()
            self.lastrowid = row[0] if row else None
            return self
        self._cursor.execute(sql, params or ())
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class PostgresConnection:
    """SQLite Connection과 비슷한 최소 인터페이스를 제공하는 Postgres 연결 래퍼."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self) -> PostgresCursor:
        return PostgresCursor(self._conn.cursor())

    def execute(self, sql: str, params: tuple | list | None = None):
        return self.cursor().execute(sql, params)

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def ensure_db(db_path: str, database_url: str | None = None):
    """실행 환경에 맞는 DB 연결을 만든다.

    - `database_url`이 있으면 Supabase/Postgres를 운영 DB로 사용한다.
    - 없으면 SQLite 파일을 사용한다. 이 경로는 로컬 개발과 테스트용이다.
    """
    if database_url:
        return ensure_postgres_db(database_url)
    return ensure_sqlite_db(db_path)


def ensure_sqlite_db(db_path: str) -> sqlite3.Connection:
    """SQLite DB와 필요한 테이블을 준비한다."""
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(db_path)
    # 수집 중 읽기/쓰기가 겹쳐도 잠금 충돌을 줄이기 위해 WAL 모드를 사용한다.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feeds (
            url TEXT PRIMARY KEY,
            category TEXT,
            publisher TEXT,
            bias_type TEXT,
            title TEXT,
            etag TEXT,
            modified_at TEXT,
            last_checked TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feed_url TEXT,
            guid TEXT,
            link TEXT,
            category TEXT,
            title TEXT,
            publisher TEXT,
            bias_type TEXT,
            published TEXT,
            summary TEXT,
            content TEXT,
            content_source TEXT,
            status TEXT,
            created_at TEXT,
            updated_at TEXT,
            -- RSS 피드마다 GUID 품질이 달라 link와 guid를 둘 다 중복 방지 키로 둔다.
            UNIQUE(link),
            UNIQUE(guid)
        )
        """
    )
    _ensure_column(conn, "feeds", "category", "TEXT")
    _ensure_column(conn, "feeds", "publisher", "TEXT")
    _ensure_column(conn, "feeds", "bias_type", "TEXT")
    _ensure_column(conn, "feeds", "modified_at", "TEXT")
    _ensure_column(conn, "articles", "category", "TEXT")
    _ensure_column(conn, "articles", "publisher", "TEXT")
    _ensure_column(conn, "articles", "bias_type", "TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS article_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER UNIQUE REFERENCES articles(id) ON DELETE CASCADE,
            status TEXT,
            attempts INTEGER,
            last_error TEXT,
            last_attempt_at TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS article_ai_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            article_id INTEGER UNIQUE REFERENCES articles(id) ON DELETE CASCADE,
            summary TEXT,
            abuse_score REAL,
            abuse_label TEXT,
            keywords TEXT,
            status TEXT,
            last_error TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    return conn


def ensure_postgres_db(database_url: str) -> PostgresConnection:
    """Supabase/Postgres DB와 필요한 테이블을 준비한다.

    Supabase는 Postgres 기반이므로 별도 Supabase SDK 없이 psycopg로 직접 저장한다.
    서버에서는 `DATABASE_URL` 환경변수에 Supabase connection string을 넣어 사용한다.
    """
    if psycopg is None:
        raise RuntimeError("psycopg is required for Supabase/Postgres. Install requirements.txt first.")

    conn = PostgresConnection(psycopg.connect(database_url))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS feeds (
            url TEXT PRIMARY KEY,
            category TEXT,
            publisher TEXT,
            bias_type TEXT,
            title TEXT,
            etag TEXT,
            modified_at TEXT,
            last_checked TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS articles (
            id BIGSERIAL PRIMARY KEY,
            feed_url TEXT,
            guid TEXT UNIQUE,
            link TEXT UNIQUE,
            category TEXT,
            title TEXT,
            publisher TEXT,
            bias_type TEXT,
            published TEXT,
            summary TEXT,
            content TEXT,
            content_source TEXT,
            status TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    _ensure_postgres_column(conn, "feeds", "category", "TEXT")
    _ensure_postgres_column(conn, "feeds", "publisher", "TEXT")
    _ensure_postgres_column(conn, "feeds", "bias_type", "TEXT")
    _ensure_postgres_column(conn, "feeds", "modified_at", "TEXT")
    _ensure_postgres_column(conn, "articles", "category", "TEXT")
    _ensure_postgres_column(conn, "articles", "publisher", "TEXT")
    _ensure_postgres_column(conn, "articles", "bias_type", "TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS article_jobs (
            id BIGSERIAL PRIMARY KEY,
            article_id BIGINT UNIQUE REFERENCES articles(id) ON DELETE CASCADE,
            status TEXT,
            attempts INTEGER,
            last_error TEXT,
            last_attempt_at TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS article_ai_results (
            id BIGSERIAL PRIMARY KEY,
            article_id BIGINT UNIQUE REFERENCES articles(id) ON DELETE CASCADE,
            summary TEXT,
            abuse_score DOUBLE PRECISION,
            abuse_label TEXT,
            keywords JSONB,
            status TEXT,
            last_error TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    conn.commit()
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    """기존 SQLite DB에 새 컬럼이 없으면 추가한다."""
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def _ensure_postgres_column(conn: PostgresConnection, table: str, column: str, column_type: str) -> None:
    """기존 Postgres 테이블에 새 컬럼이 없으면 추가한다."""
    conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {column_type}")


def _to_postgres_sql(sql: str) -> str:
    """SQLite placeholder를 Postgres placeholder로 바꾼다."""
    return sql.replace("?", "%s")


def _is_article_insert(sql: str) -> bool:
    """articles INSERT는 생성된 article id가 필요하므로 별도 처리한다."""
    return sql.lstrip().upper().startswith("INSERT INTO ARTICLES")


def backfill_article_categories(conn) -> None:
    """이미 저장된 기사 중 category가 비어 있는 행을 피드 기준으로 보정한다."""
    # 피드 메타데이터가 채워진 뒤 기존 기사에도 같은 값을 반영한다.
    conn.execute(
        """
        UPDATE articles
        SET category = (
            SELECT feeds.category
            FROM feeds
            WHERE feeds.url = articles.feed_url
        )
        WHERE category IS NULL
          AND EXISTS (
              SELECT 1
              FROM feeds
              WHERE feeds.url = articles.feed_url
                AND feeds.category IS NOT NULL
          )
        """
    )
    conn.execute(
        """
        UPDATE articles
        SET publisher = (
            SELECT feeds.publisher
            FROM feeds
            WHERE feeds.url = articles.feed_url
        )
        WHERE publisher IS NULL
          AND EXISTS (
              SELECT 1
              FROM feeds
              WHERE feeds.url = articles.feed_url
                AND feeds.publisher IS NOT NULL
          )
        """
    )
    conn.execute(
        """
        UPDATE articles
        SET bias_type = (
            SELECT feeds.bias_type
            FROM feeds
            WHERE feeds.url = articles.feed_url
        )
        WHERE bias_type IS NULL
          AND EXISTS (
              SELECT 1
              FROM feeds
              WHERE feeds.url = articles.feed_url
                AND feeds.bias_type IS NOT NULL
          )
        """
    )
    conn.commit()


def now_iso() -> str:
    """DB에 저장할 UTC ISO timestamp를 만든다."""
    # 외부 시스템과 주고받기 쉬운 UTC ISO 문자열로 저장한다.
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def normalize_published(value: str | None) -> str | None:
    """RSS별로 제각각인 published/updated 값을 가능한 경우 ISO 문자열로 표준화한다."""
    if not value:
        return None
    if date_parser:
        try:
            # 피드마다 날짜 형식이 달라 dateutil로 가능한 범위만 표준화한다.
            return date_parser.parse(value).isoformat()
        except Exception:
            return value
    return value


def enqueue_article_job(conn, article_id: int) -> None:
    """후속 AI 처리 대상 기사를 article_jobs에 넣는다.

    `article_id`에 UNIQUE 제약이 있으므로 같은 기사가 RSS 단계와 crawl 단계에서
    여러 번 ready가 되어도 큐에는 한 번만 들어간다.
    """
    now = now_iso()
    # 같은 기사가 여러 경로로 ready가 되어도 후속 처리 작업에는 한 번만 들어간다.
    conn.execute(
        """
        INSERT INTO article_jobs (
            article_id, status, attempts, last_error, last_attempt_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(article_id) DO NOTHING
        """,
        (article_id, "pending", 0, None, None, now, now),
    )
