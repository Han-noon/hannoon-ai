"""Command-line entry point for DB-backed Subtopic classification."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from collector.storage import PostgresConnection

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
    parser.add_argument(
        "--replay-topic",
        action="store_true",
        help="Explicitly replay every Event in --topic-id; never enabled automatically",
    )
    parser.add_argument(
        "--profiles-only",
        action="store_true",
        help="Generate/reuse Event Profiles without classification or processed markers",
    )
    parser.add_argument("--import-checkpoint", type=Path,
        help="Validate/adopt an explicitly reconciled checkpoint JSON for --topic-id; no AI calls")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--json-mode",
        action="store_true",
        help="Request JSON response mode from Profile/Subtopic model providers",
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
    if args.replay_topic and (
        args.topic_id is None or args.event_id is not None or args.limit is not None
    ):
        parser.error("--replay-topic requires --topic-id and cannot use --event-id/--limit")
    if args.replay_topic and args.profiles_only:
        parser.error("--replay-topic and --profiles-only cannot be combined")
    if args.import_checkpoint is not None and (
        args.topic_id is None or args.event_id is not None or args.limit is not None
        or args.replay_topic or args.profiles_only
    ):
        parser.error("--import-checkpoint requires --topic-id and cannot be combined with other modes")
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
        import psycopg
        from psycopg.rows import dict_row

        # Subtopic-only path: validate only its own schema (no collector jobs/feeds).
        with PostgresConnection(psycopg.connect(database_url, row_factory=dict_row, autocommit=True)) as conn:
            service = SubtopicBatchService(
                conn,
                env_file=args.env_file,
                profile_model=args.profile_model,
                subtopic_model=args.subtopic_model,
                summary_model=args.summary_model,
                json_mode=args.json_mode,
                dry_run=args.dry_run,
            )
            if args.import_checkpoint is not None:
                checkpoint = json.loads(args.import_checkpoint.read_text(encoding="utf-8-sig"))
                result = service.import_checkpoint(args.topic_id, checkpoint)
            elif args.profiles_only:
                result = service.generate_profiles_only(
                    topic_id=args.topic_id,
                    event_id=args.event_id,
                    limit=args.limit,
                )
            else:
                result = service.run(
                    topic_id=args.topic_id,
                    event_id=args.event_id,
                    limit=args.limit,
                    replay_topic=args.replay_topic,
                )
    except Exception as exc:
        logging.getLogger(__name__).exception("Subtopic batch failed")
        print(f"[subtopic] failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 1 if result.get("failures") or result.get("summary_failures") else 0


if __name__ == "__main__":
    raise SystemExit(main())
