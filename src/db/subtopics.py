"""Repository operations for the DB-backed Subtopic classifier.

The production schema is managed outside this application.  This module only
validates the tables it needs and provides SQL operations; it never creates or
alters production tables.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from subtopic_classifier.models import ArticleRow, EventRow, ProfileResult


_REQUIRED_COLUMNS: dict[str, set[str]] = {
    "topics": {"id", "title"},
    "events": {
        "id",
        "topic_id",
        "title",
        "created_at",
        "updated_at",
        "subtopic_processed_at",
    },
    "articles": {
        "id",
        "title",
        "publisher",
        "published_at",
        "link",
        "content",
        "updated_at",
    },
    "event_articles": {"event_id", "article_id"},
    "event_profiles": {
        "event_id",
        "event_summary",
        "anchors",
        "input_hash",
        "prompt_version",
        "created_at",
        "updated_at",
    },
    "subtopics": {
        "id",
        "topic_id",
        "name",
        "common_basis",
        "membership_criterion",
        "summary",
        "type",
        "created_at",
        "updated_at",
    },
    "subtopic_events": {"id", "subtopic_id", "event_id", "created_at"},
}


def validate_schema(conn) -> None:
    """Fail early if the externally managed Postgres schema is incomplete."""

    rows = conn.query(
        """
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = ANY(?::text[])
        """,
        (list(_REQUIRED_COLUMNS),),
    )
    actual: dict[str, set[str]] = {table: set() for table in _REQUIRED_COLUMNS}
    for row in rows:
        actual.setdefault(row["table_name"], set()).add(row["column_name"])

    missing_tables = [table for table, columns in actual.items() if not columns]
    missing_columns = {
        table: sorted(required - actual.get(table, set()))
        for table, required in _REQUIRED_COLUMNS.items()
        if required - actual.get(table, set())
    }
    if not missing_tables and not missing_columns:
        return

    details: list[str] = []
    if missing_tables:
        details.append("missing tables: " + ", ".join(sorted(missing_tables)))
    for table, columns in sorted(missing_columns.items()):
        if table not in missing_tables:
            details.append(f"{table} missing columns: {', '.join(columns)}")
    raise RuntimeError("Subtopic database schema is incomplete (" + "; ".join(details) + ")")


def _event_from_row(row: dict[str, Any]) -> EventRow:
    return EventRow(
        id=int(row["id"]),
        topic_id=int(row["topic_id"]),
        topic_name=str(row["topic_name"]),
        title=str(row["title"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        subtopic_processed_at=row["subtopic_processed_at"],
    )


def _event_scope_sql(
    *,
    topic_id: int | None,
    event_id: int | None,
) -> tuple[list[str], list[Any]]:
    conditions = ["e.topic_id IS NOT NULL"]
    params: list[Any] = []
    if topic_id is not None:
        conditions.append("e.topic_id = ?")
        params.append(topic_id)
    if event_id is not None:
        conditions.append("e.id = ?")
        params.append(event_id)
    return conditions, params


def fetch_bootstrap_events(
    conn,
    *,
    topic_id: int | None = None,
    event_id: int | None = None,
) -> list[EventRow]:
    conditions, params = _event_scope_sql(topic_id=topic_id, event_id=event_id)
    rows = conn.query(
        f"""
        SELECT e.id, e.topic_id, t.title AS topic_name, e.title,
               e.created_at, e.updated_at, e.subtopic_processed_at
        FROM events e
        JOIN topics t ON t.id = e.topic_id
        WHERE {' AND '.join(conditions)}
        ORDER BY e.created_at ASC, e.id ASC
        """,
        params,
    )
    return [_event_from_row(row) for row in rows]


def fetch_pending_events(
    conn,
    *,
    prompt_version: str,
    topic_id: int | None = None,
    event_id: int | None = None,
    force: bool = False,
    limit: int | None = None,
) -> list[EventRow]:
    """Return Events whose exact Subtopic input may need re-evaluation.

    The article-id digest is the middle component of the ``v2:<md5>:<sha256>``
    input hash built by ``prepare_event``.  Comparing it in SQL catches links
    added to or removed from ``event_articles``, which has no updated timestamp.
    The service performs the final full hash comparison after loading the rows.
    """

    conditions, params = _event_scope_sql(topic_id=topic_id, event_id=event_id)
    if not force:
        conditions.append(
            """
            (
                e.subtopic_processed_at IS NULL
                OR ep.event_id IS NULL
                OR ep.prompt_version IS DISTINCT FROM ?
                OR e.updated_at > e.subtopic_processed_at
                OR EXISTS (
                    SELECT 1
                    FROM event_articles changed_ea
                    JOIN articles changed_a ON changed_a.id = changed_ea.article_id
                    WHERE changed_ea.event_id = e.id
                      AND changed_a.updated_at > e.subtopic_processed_at
                )
                OR split_part(COALESCE(ep.input_hash, ''), ':', 2)
                   IS DISTINCT FROM COALESCE(
                       (
                           SELECT md5(string_agg(linked.article_id::text, ',' ORDER BY linked.article_id))
                           FROM (
                               SELECT DISTINCT ea.article_id
                               FROM event_articles ea
                               WHERE ea.event_id = e.id
                           ) linked
                       ),
                       md5('')
                   )
            )
            """
        )
        params.append(prompt_version)

    limit_sql = ""
    if limit is not None:
        if limit <= 0:
            raise ValueError("limit must be greater than 0")
        limit_sql = "LIMIT ?"
        params.append(limit)

    rows = conn.query(
        f"""
        SELECT e.id, e.topic_id, t.title AS topic_name, e.title,
               e.created_at, e.updated_at, e.subtopic_processed_at
        FROM events e
        JOIN topics t ON t.id = e.topic_id
        LEFT JOIN event_profiles ep ON ep.event_id = e.id
        WHERE {' AND '.join(conditions)}
        ORDER BY e.created_at ASC, e.id ASC
        {limit_sql}
        """,
        params,
    )
    return [_event_from_row(row) for row in rows]


def fetch_event(conn, event_id: int) -> EventRow | None:
    row = conn.query_one(
        """
        SELECT e.id, e.topic_id, t.title AS topic_name, e.title,
               e.created_at, e.updated_at, e.subtopic_processed_at
        FROM events e
        JOIN topics t ON t.id = e.topic_id
        WHERE e.id = ?
          AND e.topic_id IS NOT NULL
        """,
        (event_id,),
    )
    return _event_from_row(row) if row else None


def fetch_topic_events(conn, topic_id: int) -> list[EventRow]:
    rows = conn.query(
        """
        SELECT e.id, e.topic_id, t.title AS topic_name, e.title,
               e.created_at, e.updated_at, e.subtopic_processed_at
        FROM events e
        JOIN topics t ON t.id = e.topic_id
        WHERE e.topic_id = ?
        ORDER BY e.created_at ASC, e.id ASC
        """,
        (topic_id,),
    )
    return [_event_from_row(row) for row in rows]


def fetch_event_articles(conn, event_id: int) -> list[ArticleRow]:
    rows = conn.query(
        """
        SELECT linked.id, linked.title, linked.publisher, linked.published_at,
               linked.link, linked.content, linked.updated_at
        FROM (
            SELECT DISTINCT a.id, a.title, a.publisher, a.published_at,
                            a.link, a.content, a.updated_at
            FROM event_articles ea
            JOIN articles a ON a.id = ea.article_id
            WHERE ea.event_id = ?
        ) linked
        ORDER BY linked.published_at ASC, linked.id ASC
        """,
        (event_id,),
    )
    return [
        ArticleRow(
            id=int(row["id"]),
            title=str(row["title"]),
            publisher=str(row["publisher"]),
            published_at=row["published_at"],
            link=str(row["link"]),
            content=str(row["content"] or ""),
            updated_at=row["updated_at"],
        )
        for row in rows
    ]


def fetch_event_profile(conn, event_id: int) -> dict[str, Any] | None:
    return conn.query_one(
        """
        SELECT event_id, event_summary, anchors, input_hash, prompt_version,
               created_at, updated_at
        FROM event_profiles
        WHERE event_id = ?
        """,
        (event_id,),
    )


def upsert_event_profile(conn, profile: ProfileResult) -> None:
    conn.execute(
        """
        INSERT INTO event_profiles (
            event_id, event_summary, anchors, input_hash, prompt_version
        ) VALUES (?, ?, ?::jsonb, ?, ?)
        ON CONFLICT (event_id) DO UPDATE
        SET event_summary = EXCLUDED.event_summary,
            anchors = EXCLUDED.anchors,
            input_hash = EXCLUDED.input_hash,
            prompt_version = EXCLUDED.prompt_version,
            updated_at = now()
        """,
        (
            profile.event_id,
            profile.event_summary,
            json.dumps(profile.anchors, ensure_ascii=False),
            profile.input_hash,
            profile.prompt_version,
        ),
    )


def adopt_event_profile_metadata(
    conn,
    *,
    event_id: int,
    input_hash: str,
    prompt_version: str,
) -> None:
    conn.execute(
        """
        UPDATE event_profiles
        SET input_hash = ?, prompt_version = ?, updated_at = now()
        WHERE event_id = ?
        """,
        (input_hash, prompt_version, event_id),
    )


def count_unbootstrapped_profiles(conn, *, topic_id: int | None = None) -> int:
    conditions = ["e.topic_id IS NOT NULL", "(ep.input_hash IS NULL OR ep.prompt_version IS NULL)"]
    params: list[Any] = []
    if topic_id is not None:
        conditions.append("e.topic_id = ?")
        params.append(topic_id)
    row = conn.query_one(
        f"""
        SELECT count(*) AS count
        FROM event_profiles ep
        JOIN events e ON e.id = ep.event_id
        WHERE {' AND '.join(conditions)}
        """,
        params,
    )
    return int(row["count"]) if row else 0


def event_membership_count(conn, event_id: int) -> int:
    row = conn.query_one(
        "SELECT count(*) AS count FROM subtopic_events WHERE event_id = ?",
        (event_id,),
    )
    return int(row["count"]) if row else 0


def fetch_topic_subtopics(conn, topic_id: int) -> list[dict[str, Any]]:
    return conn.query(
        """
        SELECT s.id, s.topic_id, s.name, s.common_basis,
               s.membership_criterion, s.summary, s.type,
               COALESCE(
                   array_agg(se.event_id ORDER BY e.created_at, e.id)
                       FILTER (WHERE se.event_id IS NOT NULL),
                   ARRAY[]::bigint[]
               ) AS event_ids
        FROM subtopics s
        LEFT JOIN subtopic_events se ON se.subtopic_id = s.id
        LEFT JOIN events e ON e.id = se.event_id
        WHERE s.topic_id = ?
        GROUP BY s.id
        ORDER BY s.id ASC
        """,
        (topic_id,),
    )


def insert_subtopic(conn, topic_id: int, item: dict[str, Any]) -> int:
    row = conn.query_one(
        """
        INSERT INTO subtopics (
            topic_id, name, common_basis, membership_criterion, type
        ) VALUES (?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            topic_id,
            item["name"],
            item.get("common_basis"),
            item.get("membership_criterion"),
            item["type"],
        ),
    )
    if not row:
        raise RuntimeError("subtopic insert did not return an id")
    return int(row["id"])


def update_subtopic(
    conn,
    db_id: int,
    topic_id: int,
    item: dict[str, Any],
) -> None:
    conn.execute(
        """
        UPDATE subtopics
        SET summary = CASE
                WHEN (name, common_basis, membership_criterion, type)
                     IS DISTINCT FROM (?, ?, ?, ?)
                THEN NULL
                ELSE summary
            END,
            name = ?,
            common_basis = ?,
            membership_criterion = ?,
            type = ?,
            updated_at = now()
        WHERE id = ? AND topic_id = ?
        """,
        (
            item["name"],
            item.get("common_basis"),
            item.get("membership_criterion"),
            item["type"],
            item["name"],
            item.get("common_basis"),
            item.get("membership_criterion"),
            item["type"],
            db_id,
            topic_id,
        ),
    )


def delete_subtopic(conn, db_id: int, topic_id: int) -> None:
    # The supplied schema has no ON DELETE CASCADE, so remove memberships first.
    conn.execute(
        """
        DELETE FROM subtopic_events se
        USING subtopics s
        WHERE se.subtopic_id = s.id
          AND s.id = ?
          AND s.topic_id = ?
        """,
        (db_id, topic_id),
    )
    conn.execute("DELETE FROM subtopics WHERE id = ? AND topic_id = ?", (db_id, topic_id))


def sync_memberships(
    conn,
    *,
    topic_id: int,
    db_subtopic_id: int,
    event_ids: Iterable[int],
) -> None:
    requested = {int(value) for value in event_ids}
    owner = conn.query_one(
        "SELECT id FROM subtopics WHERE id = ? AND topic_id = ?",
        (db_subtopic_id, topic_id),
    )
    if owner is None:
        raise RuntimeError(
            f"subtopic_id={db_subtopic_id} does not belong to topic_id={topic_id}"
        )

    valid: set[int] = set()
    if requested:
        placeholders = ", ".join("?" for _ in requested)
        rows = conn.query(
            f"SELECT id FROM events WHERE topic_id = ? AND id IN ({placeholders})",
            (topic_id, *sorted(requested)),
        )
        valid = {int(row["id"]) for row in rows}
    invalid = sorted(requested - valid)
    if invalid:
        raise RuntimeError(
            f"subtopic_id={db_subtopic_id} contains Events outside topic_id={topic_id}: "
            + ", ".join(str(value) for value in invalid)
        )

    rows = conn.query(
        "SELECT id, event_id FROM subtopic_events WHERE subtopic_id = ? ORDER BY id",
        (db_subtopic_id,),
    )
    current = {int(row["event_id"]) for row in rows}
    if current == requested and len(rows) == len(current):
        return

    # Replacing the small membership set also cleans up legacy duplicate rows.
    conn.execute("DELETE FROM subtopic_events WHERE subtopic_id = ?", (db_subtopic_id,))
    for event_id in sorted(requested):
        conn.execute(
            "INSERT INTO subtopic_events (subtopic_id, event_id) VALUES (?, ?)",
            (db_subtopic_id, event_id),
        )
    conn.execute(
        "UPDATE subtopics SET summary = NULL, updated_at = now() WHERE id = ? AND topic_id = ?",
        (db_subtopic_id, topic_id),
    )


def remove_event_memberships(conn, event_id: int) -> None:
    conn.execute("DELETE FROM subtopic_events WHERE event_id = ?", (event_id,))


def invalidate_event_subtopic_summaries(conn, event_id: int) -> None:
    conn.execute(
        """
        UPDATE subtopics s
        SET summary = NULL, updated_at = now()
        WHERE EXISTS (
            SELECT 1
            FROM subtopic_events se
            WHERE se.subtopic_id = s.id
              AND se.event_id = ?
        )
        """,
        (event_id,),
    )


def fetch_topics_with_missing_summaries(
    conn,
    *,
    topic_id: int | None = None,
) -> list[int]:
    conditions = ["s.summary IS NULL"]
    params: list[Any] = []
    if topic_id is not None:
        conditions.append("s.topic_id = ?")
        params.append(topic_id)
    rows = conn.query(
        f"""
        SELECT DISTINCT s.topic_id
        FROM subtopics s
        WHERE {' AND '.join(conditions)}
        ORDER BY s.topic_id
        """,
        params,
    )
    return [int(row["topic_id"]) for row in rows]


def fetch_subtopics_needing_summary(conn, topic_id: int) -> list[dict[str, Any]]:
    return [
        row
        for row in fetch_topic_subtopics(conn, topic_id)
        if row.get("summary") is None and row.get("event_ids")
    ]


def update_subtopic_summary(
    conn,
    *,
    db_id: int,
    topic_id: int,
    summary: str,
) -> None:
    conn.execute(
        """
        UPDATE subtopics
        SET summary = ?, updated_at = now()
        WHERE id = ? AND topic_id = ?
        """,
        (summary, db_id, topic_id),
    )


def lock_topic(conn, topic_id: int) -> None:
    conn.query_one("SELECT id FROM topics WHERE id = ? FOR UPDATE", (topic_id,))


def lock_event(conn, event_id: int) -> None:
    conn.query_one("SELECT id FROM events WHERE id = ? FOR UPDATE", (event_id,))


def mark_events_processed(conn, event_ids: Iterable[int]) -> None:
    ids = sorted({int(value) for value in event_ids})
    if not ids:
        return
    placeholders = ", ".join("?" for _ in ids)
    conn.execute(
        f"UPDATE events SET subtopic_processed_at = now() WHERE id IN ({placeholders})",
        ids,
    )


def try_topic_advisory_lock(conn, topic_id: int) -> bool:
    row = conn.query_one(
        "SELECT pg_try_advisory_lock(hashtextextended('hannoon-subtopic:' || ?::text, 0)) AS locked",
        (topic_id,),
    )
    return bool(row and row["locked"])


def release_topic_advisory_lock(conn, topic_id: int) -> None:
    conn.query_one(
        "SELECT pg_advisory_unlock(hashtextextended('hannoon-subtopic:' || ?::text, 0)) AS unlocked",
        (topic_id,),
    )
