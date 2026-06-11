"""토픽 분류 파이프라인 CLI.

collector.cli와 동일한 형식으로, settings의 상수를 명령행 인자로 노출한다.
토픽 분류는 pgvector가 필요하므로 Postgres 연결(--database-url 또는 DATABASE_URL)이 필수다.
"""

import argparse
import os
import sys

from dotenv import load_dotenv

from collector.settings import DEFAULT_DB
from collector.storage import ensure_db
from topic_classifier.pipeline import run
from topic_classifier.settings import (
    BATCH_SIZE,
    LLM_MODEL,
    MIN_NET_ARTICLE_COUNT,
    TOP_K,
)


def build_parser() -> argparse.ArgumentParser:
    """명령행 파서를 구성한다. 기본값은 settings의 상수를 사용한다."""
    parser = argparse.ArgumentParser(description="미배정 이벤트를 토픽에 배정하는 분류 파이프라인")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="Postgres/Supabase database URL. Defaults to DATABASE_URL env var",
    )
    parser.add_argument(
        "--min-net-article-count",
        type=int,
        default=MIN_NET_ARTICLE_COUNT,
        help="실제 기사 수((article_count - abusing_count)) 최소값. 미달 이벤트는 분류 제외",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_SIZE,
        help="한 번의 실행에서 처리할 최대 이벤트 수",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=TOP_K,
        help="토픽 후보 검색 시 반환할 최대 토픽 수",
    )
    parser.add_argument(
        "--llm-model",
        default=LLM_MODEL,
        help="cause/result 추출·배정·최신화에 사용할 LLM 모델",
    )
    return parser


def main() -> int:
    """CLI 인자를 해석하고 토픽 분류 파이프라인을 실행한다."""
    load_dotenv()
    args = build_parser().parse_args()

    if args.min_net_article_count < 0:
        print("--min-net-article-count는 0 이상이어야 합니다")
        return 1
    if args.batch_size <= 0:
        print("--batch-size는 0보다 커야 합니다")
        return 1
    if args.top_k <= 0:
        print("--top-k는 0보다 커야 합니다")
        return 1

    if not args.database_url:
        print(
            "[topic] Postgres 연결이 필요합니다 --database-url 또는 DATABASE_URL 환경변수를 설정하세요\n"
            "토픽 분류는 pgvector가 필요하므로 Postgres 연결이 필수입니다",
            file=sys.stderr,
        )
        return 1

    with ensure_db(DEFAULT_DB, database_url=args.database_url) as conn:
        count = run(
            conn,
            args.min_net_article_count,
            args.batch_size,
            args.top_k,
            args.llm_model,
        )
        print(f"[done] 토픽 배정 완료: {count}건")

    return 0
