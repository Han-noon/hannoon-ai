import argparse
import os

from dotenv import load_dotenv

from .crawler import crawl_articles
from .rss import fetch_feed
from .settings import (
    DEFAULT_ABUSE_P1_MODEL_DIR,
    DEFAULT_ABUSE_P2_MODEL_DIR,
    DEFAULT_CLASSIFY_BATCH_SIZE,
    DEFAULT_CLASSIFY_MAX_ATTEMPTS,
    DEFAULT_CRAWL_BATCH_SIZE,
    DEFAULT_DB,
    DEFAULT_DOMAIN_DELAY,
    DEFAULT_FEEDS_FILE,
    DEFAULT_LLM_CLEANUP_MODEL,
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
    parser.add_argument("--feeds-file", default=DEFAULT_FEEDS_FILE, help="JSON file of feed URLs")
    parser.add_argument("--feed", action="append", help="Extra feed URL or local file")
    parser.add_argument("--min-rss-len", type=int, default=DEFAULT_MIN_RSS_LEN)
    parser.add_argument("--min-crawl-len", type=int, default=DEFAULT_MIN_CRAWL_LEN)
    parser.add_argument(
        "--crawl-batch-size",
        type=int,
        default=DEFAULT_CRAWL_BATCH_SIZE,
        help="Number of articles to load per crawl batch",
    )
    parser.add_argument(
        "--classify-batch-size",
        type=int,
        default=DEFAULT_CLASSIFY_BATCH_SIZE,
        help="Number of pending articles to classify per batch",
    )
    parser.add_argument(
        "--classify-max-attempts",
        type=int,
        default=DEFAULT_CLASSIFY_MAX_ATTEMPTS,
        help="Maximum classification attempts before marking a job failed",
    )
    parser.add_argument(
        "--classify-after-crawl",
        action="store_true",
        help="Run abuse classification after crawl when command is run",
    )
    parser.add_argument(
        "--abuse-p1-model-dir",
        default=os.environ.get("ABUSE_P1_MODEL_DIR", DEFAULT_ABUSE_P1_MODEL_DIR),
        help="Path to p1 clickbait model directory",
    )
    parser.add_argument(
        "--abuse-p2-model-dir",
        default=os.environ.get("ABUSE_P2_MODEL_DIR", DEFAULT_ABUSE_P2_MODEL_DIR),
        help="Path to p2 topic mismatch model directory",
    )
    parser.add_argument("--abuse-device", default="auto", help="Inference device: auto, cpu, cuda, cuda:0, etc.")
    parser.add_argument("--llm-cleanup", action="store_true", help="Use an LLM only for crawled text that looks noisy")
    parser.add_argument("--llm-cleanup-model", default=DEFAULT_LLM_CLEANUP_MODEL, help="OpenAI model for --llm-cleanup")
    parser.add_argument("--domain-delay", type=float, default=DEFAULT_DOMAIN_DELAY)
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
    subparsers.add_parser("fetch", help="Fetch RSS items only")
    subparsers.add_parser("crawl", help="Crawl items that need full text")
    subparsers.add_parser("classify", help="Classify ready articles for abuse")
    subparsers.add_parser("run", help="Fetch RSS and crawl missing text")
    return parser


def main() -> int:
    """CLI 인자를 해석하고 수집 파이프라인을 실행한다.

    처리 순서는 `DB 준비 -> RSS 수집 -> 카테고리 보정 -> 원문 크롤링`이다.
    `fetch`만 실행할 때는 크롤링을 건너뛰고, `crawl`만 실행할 때는 이미 DB에
    저장된 `needs_crawl` 기사만 처리한다.
    """
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()
    # 운영/테스트에서 명령을 생략해도 전체 파이프라인이 돌도록 기본값은 run으로 둔다.
    command = args.command or "run"

    feed_urls = load_feed_urls(args.feeds_file, args.feed)
    if command in {"fetch", "run"} and not feed_urls:
        print("No feeds configured. Provide --feed or config/feeds.json")
        return 1

    if args.crawl_batch_size <= 0:
        print("--crawl-batch-size must be greater than 0")
        return 1
    if args.classify_batch_size <= 0:
        print("--classify-batch-size must be greater than 0")
        return 1
    if args.classify_max_attempts <= 0:
        print("--classify-max-attempts must be greater than 0")
        return 1

    text_cleaner = None

    # database_url이 있으면 Supabase/Postgres, 없으면 로컬 SQLite 연결을 만든다.
    with ensure_db(args.db, database_url=args.database_url) as conn:
        if command in {"fetch", "run"}:
            total = 0
            for feed_url in feed_urls:
                # 피드는 서로 독립적으로 처리해 한 피드의 신규 건수만 로그에서 바로 확인할 수 있게 한다.
                # 실패한 피드의 부분 쓰기는 fetch_feed의 transaction() 블록이 이미 롤백하므로,
                # 여기서는 로그만 남기고 다음 피드로 넘어간다.
                try:
                    count = fetch_feed(conn, feed_url, args.min_rss_len, offline=args.offline)
                except Exception as exc:
                    print(f"[warn] feed failed: {feed_url} ({exc})")
                    continue
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
                crawl_batch_size=args.crawl_batch_size,
                llm_cleanup=args.llm_cleanup,
                llm_cleanup_model=args.llm_cleanup_model,
                text_cleaner=text_cleaner,
            )
            print(f"[done] Crawled items updated: {updated}")

        if command == "classify" or (command == "run" and args.classify_after_crawl):
            from .ai_worker import classify_pending_articles

            classified = classify_pending_articles(
                conn,
                p1_model_dir=args.abuse_p1_model_dir,
                p2_model_dir=args.abuse_p2_model_dir,
                batch_size=args.classify_batch_size,
                device=args.abuse_device,
                max_attempts=args.classify_max_attempts,
            )
            print(f"[done] Classified items: {classified}")

    return 0
