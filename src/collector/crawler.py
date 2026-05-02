import time
from urllib.parse import urlparse

import requests

from .settings import USER_AGENT
from .storage import enqueue_article_job, now_iso
from .utils import extract_article_text


def fetch_url_text(url: str, offline: bool) -> str | None:
    """기사 URL에서 본문 텍스트를 추출한다.

    HTTP/HTTPS URL은 requests로 가져오고, file:// URL은 로컬 HTML 파일을 읽는다.
    로컬 파일 지원은 네트워크 없이도 샘플 RSS와 샘플 HTML로 회귀 테스트를 할 수
    있게 하기 위한 장치다.
    """
    parsed = urlparse(url)
    if parsed.scheme == "file":
        # 오프라인 테스트에서는 RSS 항목의 링크가 로컬 HTML 파일을 가리킬 수 있다.
        local_path = parsed.path.lstrip("/")
        with open(local_path, "r", encoding="utf-8") as handle:
            html = handle.read()
        return extract_article_text(html)

    if offline:
        print(f"[skip] offline mode: {url}")
        return None

    headers = {"User-Agent": USER_AGENT}
    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()
    # 국내 언론사는 응답 헤더의 charset이 비어 있거나 부정확한 경우가 있어 requests 추정값을 우선한다.
    response.encoding = response.apparent_encoding or response.encoding
    return extract_article_text(response.text)


def crawl_articles(conn, min_crawl_len: int, offline: bool, domain_delay: float) -> int:
    """`needs_crawl` 상태의 기사 원문을 가져와 DB에 반영한다.

    크롤링 결과가 `min_crawl_len`보다 길면 실제 본문을 확보했다고 보고
    `ready`로 바꾼다. 너무 짧은 텍스트는 제목/캡션/광고 영역만 잡혔을 가능성이
    높으므로 `crawl_failed`로 남긴다. 개별 기사 실패는 전체 배치를 중단하지 않고
    다음 기사로 넘어간다.
    """
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT id, link FROM articles WHERE status = 'needs_crawl' AND link IS NOT NULL"
    ).fetchall()
    last_hit: dict[str, float] = {}
    updated = 0

    for article_id, link in rows:
        parsed = urlparse(link)
        if parsed.scheme in {"http", "https"}:
            # 같은 도메인에 연속 요청하지 않도록 최소 간격을 둔다.
            host = parsed.netloc
            last_time = last_hit.get(host, 0)
            wait = domain_delay - (time.time() - last_time)
            if wait > 0:
                time.sleep(wait)
            last_hit[host] = time.time()

        try:
            text = fetch_url_text(link, offline=offline)
        except Exception as exc:
            # 개별 기사 실패가 전체 배치를 멈추지 않도록 상태만 남기고 다음 기사로 넘어간다.
            print(f"[warn] crawl failed: {link} ({exc})")
            cur.execute(
                "UPDATE articles SET status = ?, updated_at = ? WHERE id = ?",
                ("crawl_failed", now_iso(), article_id),
            )
            continue

        if text and len(text) >= min_crawl_len:
            # 충분한 본문을 확보한 기사만 후속 AI 처리 큐로 넘긴다.
            cur.execute(
                """
                UPDATE articles
                SET content = ?, content_source = ?, status = ?, updated_at = ?
                WHERE id = ?
                """,
                (text, "crawl", "ready", now_iso(), article_id),
            )
            enqueue_article_job(conn, article_id)
            updated += 1
        else:
            # 너무 짧은 본문은 광고/캡션/요약만 잡힌 가능성이 높아 실패로 분류한다.
            cur.execute(
                "UPDATE articles SET status = ?, updated_at = ? WHERE id = ?",
                ("crawl_failed", now_iso(), article_id),
            )

    conn.commit()
    return updated

