import argparse
import os

from dotenv import load_dotenv

from .crawler import crawl_articles
from .rss import fetch_feed
from .settings import (
    DEFAULT_AI_BATCH_SIZE,
    DEFAULT_ABUSE_P1_MODEL_DIR,
    DEFAULT_ABUSE_P2_MODEL_DIR,
    DEFAULT_CLASSIFY_MAX_ATTEMPTS,
    DEFAULT_CRAWL_BATCH_SIZE,
    DEFAULT_DB,
    DEFAULT_DOMAIN_DELAY,
    DEFAULT_FEEDS_FILE,
    DEFAULT_LLM_CLEANUP_MODEL,
    DEFAULT_MIN_CRAWL_LEN,
    DEFAULT_SUMMARY_HEAD_CANDIDATES,
    DEFAULT_SUMMARY_MAX_ATTEMPTS,
    DEFAULT_SUMMARY_MAX_CANDIDATES,
    DEFAULT_SUMMARY_MIDDLE_CANDIDATES,
    DEFAULT_SUMMARY_MODEL_PATH,
    DEFAULT_SUMMARY_SENTENCES,
    DEFAULT_SUMMARY_TAIL_CANDIDATES,
    DEFAULT_SUMMARY_TOKENIZER_DIR,
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
    parser.add_argument("--min-crawl-len", type=int, default=DEFAULT_MIN_CRAWL_LEN)
    parser.add_argument(
        "--crawl-batch-size",
        type=int,
        default=DEFAULT_CRAWL_BATCH_SIZE,
        help="Number of articles to load per crawl batch",
    )
    parser.add_argument(
        "--ai-batch-size",
        type=int,
        default=DEFAULT_AI_BATCH_SIZE,
        help="Number of pending articles to load per AI pipeline batch",
    )
    parser.add_argument(
        "--classify-max-attempts",
        type=int,
        default=DEFAULT_CLASSIFY_MAX_ATTEMPTS,
        help="Maximum classification attempts before marking a job failed",
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
    parser.add_argument(
        "--summary-model-path",
        default=os.environ.get("SUMMARY_MODEL_PATH", DEFAULT_SUMMARY_MODEL_PATH),
        help="Path to BERT extractive summary model checkpoint",
    )
    parser.add_argument(
        "--summary-tokenizer-dir",
        default=os.environ.get("SUMMARY_TOKENIZER_DIR", DEFAULT_SUMMARY_TOKENIZER_DIR),
        help="Path to KLUE BERT tokenizer/config directory",
    )
    parser.add_argument("--summary-device", default="auto", help="Summary inference device: auto, cpu, cuda, etc.")
    parser.add_argument(
        "--summary-max-attempts",
        type=int,
        default=DEFAULT_SUMMARY_MAX_ATTEMPTS,
        help="Maximum summary attempts before marking a job failed",
    )
    parser.add_argument(
        "--summary-sentences",
        type=int,
        default=int(os.environ.get("SUMMARY_SENTENCES", DEFAULT_SUMMARY_SENTENCES)),
        help="Number of sentences to keep in extractive summary",
    )
    parser.add_argument(
        "--summary-max-candidates",
        type=int,
        default=int(os.environ.get("SUMMARY_MAX_CANDIDATES", DEFAULT_SUMMARY_MAX_CANDIDATES)),
        help="Maximum sentence candidates to score per article",
    )
    parser.add_argument(
        "--summary-head-candidates",
        type=int,
        default=int(os.environ.get("SUMMARY_HEAD_CANDIDATES", DEFAULT_SUMMARY_HEAD_CANDIDATES)),
        help="Number of leading sentence candidates to score per article",
    )
    parser.add_argument(
        "--summary-middle-candidates",
        type=int,
        default=int(os.environ.get("SUMMARY_MIDDLE_CANDIDATES", DEFAULT_SUMMARY_MIDDLE_CANDIDATES)),
        help="Number of middle sentence candidates to score per article",
    )
    parser.add_argument(
        "--summary-tail-candidates",
        type=int,
        default=int(os.environ.get("SUMMARY_TAIL_CANDIDATES", DEFAULT_SUMMARY_TAIL_CANDIDATES)),
        help="Number of trailing sentence candidates to score per article",
    )
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
    parser = argparse.ArgumentParser(description="RSS -> crawl -> classify -> summarize collector")
    add_common_args(parser)

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("fetch", help="Fetch RSS items only")
    subparsers.add_parser("crawl", help="Crawl items that need full text")
    subparsers.add_parser("process", help="Classify and summarize ready articles")
    subparsers.add_parser("run", help="Fetch RSS, crawl, classify, and summarize")
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
    if args.ai_batch_size <= 0:
        print("--ai-batch-size must be greater than 0")
        return 1
    if args.classify_max_attempts <= 0:
        print("--classify-max-attempts must be greater than 0")
        return 1
    if args.summary_max_attempts <= 0:
        print("--summary-max-attempts must be greater than 0")
        return 1
    if args.summary_sentences <= 0:
        print("--summary-sentences must be greater than 0")
        return 1
    if args.summary_max_candidates <= 0:
        print("--summary-max-candidates must be greater than 0")
        return 1
    if args.summary_head_candidates < 0:
        print("--summary-head-candidates must be greater than or equal to 0")
        return 1
    if args.summary_middle_candidates < 0:
        print("--summary-middle-candidates must be greater than or equal to 0")
        return 1
    if args.summary_tail_candidates < 0:
        print("--summary-tail-candidates must be greater than or equal to 0")
        return 1
    summary_candidate_total = (
        args.summary_head_candidates
        + args.summary_middle_candidates
        + args.summary_tail_candidates
    )
    if summary_candidate_total <= 0:
        print("At least one summary candidate count must be greater than 0")
        return 1
    if summary_candidate_total > args.summary_max_candidates:
        print("summary head/middle/tail candidates must not exceed --summary-max-candidates")
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
                    count = fetch_feed(conn, feed_url, offline=args.offline)
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

        if command in {"process", "run"}:
            from .article_ai_pipeline import process_pending_articles

            processed = process_pending_articles(
                conn,
                p1_model_dir=args.abuse_p1_model_dir,
                p2_model_dir=args.abuse_p2_model_dir,
                summary_model_path=args.summary_model_path,
                summary_tokenizer_dir=args.summary_tokenizer_dir,
                batch_size=args.ai_batch_size,
                sentence_count=args.summary_sentences,
                max_candidates=args.summary_max_candidates,
                head_candidates=args.summary_head_candidates,
                middle_candidates=args.summary_middle_candidates,
                tail_candidates=args.summary_tail_candidates,
                abuse_device=args.abuse_device,
                summary_device=args.summary_device,
                classify_max_attempts=args.classify_max_attempts,
                summary_max_attempts=args.summary_max_attempts,
            )
            print(f"[done] AI pipeline completed items: {processed}")

    return 0
