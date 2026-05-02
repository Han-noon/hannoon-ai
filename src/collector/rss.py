import json
from urllib.parse import urlparse

import feedparser

from .storage import enqueue_article_job, normalize_published, now_iso
from .utils import html_to_text, infer_feed_category, infer_publisher_metadata, resolve_entry_link


def encode_feed_modified(value) -> str | None:
    """feedparser modified 값을 DB에 복원 가능한 JSON 배열로 저장한다."""
    if not value:
        return None
    try:
        return json.dumps(list(value))
    except TypeError:
        return None


def decode_feed_modified(value: str | None):
    """DB에 저장된 modified_at JSON 배열을 feedparser가 기대하는 tuple로 복원한다."""
    if not value:
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, list) or len(decoded) != 9:
        return None
    try:
        return tuple(int(part) for part in decoded)
    except (TypeError, ValueError):
        return None


def fetch_feed(conn, feed_url: str, min_rss_len: int, offline: bool) -> int:
    """RSS 피드를 읽어 신규 기사만 저장한다.

    RSS에서 충분한 본문을 얻으면 바로 `ready` 상태로 저장하고 article_jobs에 넣는다.
    RSS 요약이 짧으면 `needs_crawl` 상태로 저장해 이후 크롤링 단계에서 원문을
    보강하게 한다. 중복 기사는 GUID를 우선하고, GUID가 없거나 불안정한 피드는
    링크를 대체 키로 사용한다.
    """
    # 이전 수집 시점의 ETag/modified_at을 넘겨 서버가 변경분만 응답할 수 있게 한다.
    cur = conn.cursor()
    row = cur.execute("SELECT etag, modified_at FROM feeds WHERE url = ?", (feed_url,)).fetchone()
    etag, modified_at = (row or (None, None))
    modified = decode_feed_modified(modified_at)

    # 오프라인 모드에서는 실수로 외부 HTTP 요청이 나가지 않도록 즉시 건너뛴다.
    if offline and urlparse(feed_url).scheme in {"http", "https"}:
        print(f"[skip] offline mode: {feed_url}")
        return 0

    # feedparser가 URL/로컬 파일을 모두 처리하므로 테스트 피드도 같은 경로로 검증할 수 있다.
    parsed = feedparser.parse(feed_url, etag=etag, modified=modified)
    if getattr(parsed, "bozo", False):
        print(f"[warn] feed parse issue: {feed_url}")

    # 피드 메타데이터는 신규 기사 여부와 무관하게 마지막 확인 시각을 갱신한다.
    feed_title = getattr(parsed.feed, "title", None)
    category = infer_feed_category(feed_url, feed_title)
    publisher, bias_type = infer_publisher_metadata(feed_url, feed_title)
    cur.execute(
        """
        INSERT INTO feeds (url, category, publisher, bias_type, title, etag, modified_at, last_checked)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            category = excluded.category,
            publisher = excluded.publisher,
            bias_type = excluded.bias_type,
            title = excluded.title,
            etag = excluded.etag,
            modified_at = excluded.modified_at,
            last_checked = excluded.last_checked
        """,
        (
            feed_url,
            category,
            publisher,
            bias_type,
            feed_title,
            getattr(parsed, "etag", None),
            encode_feed_modified(getattr(parsed, "modified", None)),
            now_iso(),
        ),
    )

    inserted = 0
    for entry in parsed.entries:
        # GUID가 가장 안정적인 식별자이고, 없는 피드는 정규화된 링크를 대체 키로 쓴다.
        guid = entry.get("id") or entry.get("guid")
        link = resolve_entry_link(entry.get("link"), feed_url)
        if not guid and not link:
            continue

        # DB UNIQUE 제약도 있지만, 사전에 건너뛰면 불필요한 예외/롤백 흐름을 피할 수 있다.
        existing = None
        if guid:
            existing = cur.execute("SELECT id FROM articles WHERE guid = ?", (guid,)).fetchone()
        if not existing and link:
            existing = cur.execute("SELECT id FROM articles WHERE link = ?", (link,)).fetchone()
        if existing:
            continue

        # RSS 본문이 URL만 담는 피드도 있어 HTML로 보이는 값만 텍스트화한다.
        content_html = ""
        if entry.get("content"):
            content_html = entry.get("content")[0].get("value", "")
        summary_html = entry.get("summary") or entry.get("description") or ""
        content_text = html_to_text(content_html) or html_to_text(summary_html)

        # RSS 요약만으로는 부족한 기사는 원문 페이지 크롤링 대상으로 표시한다.
        needs_crawl = len(content_text) < min_rss_len
        status = "needs_crawl" if needs_crawl else "ready"
        content_source = "rss" if content_text else None

        # 원본 HTML 대신 정규화된 텍스트를 저장해 후속 AI 처리 입력을 단순화한다.
        cur.execute(
            """
            INSERT INTO articles (
                feed_url, guid, link, category, title, publisher, bias_type, published, summary, content,
                content_source, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feed_url,
                guid,
                link,
                category,
                entry.get("title"),
                publisher,
                bias_type,
                normalize_published(entry.get("published") or entry.get("updated")),
                html_to_text(summary_html),
                content_text,
                content_source,
                status,
                now_iso(),
                now_iso(),
            ),
        )
        article_id = cur.lastrowid
        # RSS 단계에서 이미 충분한 본문이 있으면 크롤링 없이 바로 article_jobs에 넣는다.
        if status == "ready":
            enqueue_article_job(conn, article_id)
        inserted += 1

    conn.commit()
    return inserted
