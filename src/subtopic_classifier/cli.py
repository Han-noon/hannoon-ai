"""Command-line entry point for DB-backed Subtopic classification."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from collector.settings import DEFAULT_DB
from collector.storage import ensure_db

from .service import SubtopicBatchService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate Event Profiles and classify Events into Subtopics"
    )
    parser.add_argument(
        "--database-url",
        help="Postgres/Supabase URL; defaults to DATABASE_URL after loading --env-file",
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--topic-id", type=int)
    parser.add_argument("--event-id", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--profile-model")
    parser.add_argument("--subtopic-model")
    parser.add_argument("--summary-model")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--json-mode",
        action="store_true",
        help="Request JSON response mode from Profile/Subtopic model providers",
    )
    parser.add_argument(
        "--bootstrap-existing",
        action="store_true",
        help="Adopt existing Profiles/Subtopics without replaying the classifier",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    return parser


def _positive(parser: argparse.ArgumentParser, name: str, value: int | None) -> None:
    if value is not None and value <= 0:
        parser.error(f"{name} must be greater than 0")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    _positive(parser, "--topic-id", args.topic_id)
    _positive(parser, "--event-id", args.event_id)
    _positive(parser, "--limit", args.limit)
    if args.bootstrap_existing and (args.force or args.limit is not None):
        parser.error("--bootstrap-existing cannot be combined with --force or --limit")
    if not args.env_file.is_file():
        parser.error(f"environment file not found: {args.env_file}")

    load_dotenv(dotenv_path=args.env_file, override=False)
    database_url = args.database_url or os.getenv("DATABASE_URL")
    if not database_url:
        parser.error("--database-url or DATABASE_URL is required (Subtopics require Postgres)")

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        with ensure_db(DEFAULT_DB, database_url=database_url) as conn:
            service = SubtopicBatchService(
                conn,
                env_file=args.env_file,
                profile_model=args.profile_model,
                subtopic_model=args.subtopic_model,
                summary_model=args.summary_model,
                json_mode=args.json_mode,
                dry_run=args.dry_run,
            )
            if args.bootstrap_existing:
                result = service.bootstrap_existing(
                    topic_id=args.topic_id,
                    event_id=args.event_id,
                )
            else:
                result = service.run(
                    topic_id=args.topic_id,
                    event_id=args.event_id,
                    limit=args.limit,
                    force=args.force,
                )
    except Exception as exc:
        logging.getLogger(__name__).exception("Subtopic batch failed")
        print(f"[subtopic] failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
