import argparse
import os

from .crawler import crawl_articles
from .rss import fetch_feed
from .settings import (
    DEFAULT_DB,
    DEFAULT_DOMAIN_DELAY,
    DEFAULT_FEEDS_FILE,
    DEFAULT_MIN_CRAWL_LEN,
    DEFAULT_MIN_RSS_LEN,
)
from .storage import backfill_article_categories, ensure_db
from .utils import load_feed_urls

# CLI의 공통 옵션들을 한 번에 등록하는 설정 함수
def add_common_args(parser: argparse.ArgumentParser) -> None:
    """fetch/crawl/run 명령이 공통으로 사용하는 옵션을 등록한다.

    SQLite는 로컬 개발 기본값으로 두고, Supabase/Postgres는 `--database-url`
    또는 `DATABASE_URL` 환경변수로만 사용한다. 이렇게 분리하면 로컬 테스트 중
    운영 DB를 오염시키지 않고, 서버 배포 시에는 환경변수만 바꿔 운영 저장소로
    전환할 수 있다.
    """
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite DB path")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="Postgres/Supabase database URL. Defaults to DATABASE_URL env var.",
    )
    parser.add_argument("--feeds-file", default=DEFAULT_FEEDS_FILE, help="JSON file of feed URLs") # JSON 파일로 RSS 목록 받기
    parser.add_argument("--feed", action="append", help="Extra feed URL or local file") # feed 여러 개 받기
    parser.add_argument("--min-rss-len", type=int, default=DEFAULT_MIN_RSS_LEN) # RSS 내용 최소 길이 제한
    parser.add_argument("--min-crawl-len", type=int, default=DEFAULT_MIN_CRAWL_LEN) # 크롤링 본문 최소 길이 제한
    parser.add_argument("--domain-delay", type=float, default=DEFAULT_DOMAIN_DELAY) # 같은 사이트 요청 간격 (크롤링 속도 제한)
    parser.add_argument("--offline", action="store_true", help="Do not fetch http/https URLs")


def build_parser() -> argparse.ArgumentParser:
    """명령행 파서를 구성한다.

    - fetch: RSS만 읽고 DB에 저장한다.
    - crawl: RSS 단계에서 본문이 짧다고 판단된 기사만 원문 크롤링한다.
    - run: fetch 후 crawl까지 이어서 실행한다.
    """
    parser = argparse.ArgumentParser(description="RSS -> crawl collector")
    add_common_args(parser)

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("fetch", help="Fetch RSS items only") # RSS만 가져옴
    subparsers.add_parser("crawl", help="Crawl items that need full text") # 본문 크롤링만 수행
    subparsers.add_parser("run", help="Fetch RSS and crawl missing text") # 둘 다 수행 (통합 실행)
    return parser


def main() -> int:
    """CLI 인자를 해석하고 수집 파이프라인을 실행한다.

    처리 순서는 `DB 준비 -> RSS 수집 -> 카테고리 보정 -> 원문 크롤링`이다.
    `fetch`만 실행할 때는 크롤링을 건너뛰고, `crawl`만 실행할 때는 이미 DB에
    저장된 `needs_crawl` 기사만 처리한다.
    """
    parser = build_parser()
    args = parser.parse_args()
    # 운영/테스트에서 명령을 생략해도 전체 파이프라인이 돌도록 기본값은 run으로 둔다.
    command = args.command or "run"

    feed_urls = load_feed_urls(args.feeds_file, args.feed)
    if command in {"fetch", "run"} and not feed_urls:
        print("No feeds configured. Provide --feed or config/feeds.json")
        return 1

    # database_url이 있으면 Supabase/Postgres, 없으면 로컬 SQLite 연결을 만든다.
    with ensure_db(args.db, database_url=args.database_url) as conn:
        if command in {"fetch", "run"}:
            total = 0
            for feed_url in feed_urls:
                # 피드는 서로 독립적으로 처리해 한 피드의 신규 건수만 로그에서 바로 확인할 수 있게 한다.
                count = fetch_feed(conn, feed_url, args.min_rss_len, offline=args.offline)
                print(f"[feed] {feed_url} -> {count} new items")
                total += count
            backfill_article_categories(conn)
            print(f"[done] RSS items inserted: {total}")

        if command in {"crawl", "run"}:
            updated = crawl_articles(
                conn,
                min_crawl_len=args.min_crawl_len,
                offline=args.offline,
                domain_delay=args.domain_delay,
            )
            print(f"[done] Crawled items updated: {updated}")

    return 0

