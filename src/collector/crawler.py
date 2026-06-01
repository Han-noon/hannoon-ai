import time
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

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
        # file:// URI는 OS별 로컬 경로 규칙에 맞게 변환해야 한다.
        local_path = url2pathname(unquote(parsed.path))
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
    rows = conn.query(
        "SELECT id, link FROM articles WHERE status = 'needs_crawl' AND link IS NOT NULL"
    )
    last_hit: dict[str, float] = {}
    updated = 0

    for row in rows:
        article_id, link = row["id"], row["link"]
        parsed = urlparse(link)
        skipped_offline_http = offline and parsed.scheme in {"http", "https"}
        if skipped_offline_http:
            print(f"[skip] offline mode: {link}")
            continue
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
            conn.execute(
                "UPDATE articles SET status = ?, updated_at = ? WHERE id = ?",
                ("crawl_failed", now_iso(), article_id),
            )
            continue

        if text and len(text) >= min_crawl_len:
            # 충분한 본문을 확보한 기사만 후속 AI 처리 큐로 넘긴다.
            # 상태 UPDATE와 큐 INSERT를 한 트랜잭션으로 묶어 기사 단위로 원자적으로 커밋한다.
            with conn.transaction():
                conn.execute(
                    """
                    UPDATE articles
                    SET content = ?, content_source = ?, status = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (text, "crawl", "ready", now_iso(), article_id),
                )
                enqueue_article_job(conn, article_id)
            print(f"[crawl] {link} -> ready ({len(text)} chars)")
            updated += 1
        else:
            text_len = len(text) if text else 0
            print(f"[warn] crawl failed: {link} (content too short: {text_len} chars)")
            # 너무 짧은 본문은 광고/캡션/요약만 잡힌 가능성이 높아 실패로 분류한다.
            conn.execute(
                "UPDATE articles SET status = ?, updated_at = ? WHERE id = ?",
                ("crawl_failed", now_iso(), article_id),
            )

    return updated

