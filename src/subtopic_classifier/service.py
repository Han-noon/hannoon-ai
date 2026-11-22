from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

from db import subtopics as repo

from .engine import ValidatedSubtopicEngine, initial_state
from .models import EventRow, PreparedEvent, ProfileResult
from .profile import from_cached_profile, generate_profile, prepare_event, profile_prompt_version
from .settings import MAX_ARTICLE_SPAN_DAYS
from .summary import SubtopicSummaryGenerator

logger = logging.getLogger(__name__)


class SourceChangedError(RuntimeError):
    """The Event/article input changed while an LLM request was running."""


class EventProcessingError(RuntimeError):
    def __init__(self, event_id: int, message: str) -> None:
        super().__init__(f"event_id={event_id}: {message}")
        self.event_id = event_id


class SubtopicBatchService:
    """DB orchestration around the already-validated 17_77 classifier."""

    def __init__(
        self,
        conn,
        *,
        env_file: Path,
        profile_model: str | None = None,
        subtopic_model: str | None = None,
        summary_model: str | None = None,
        json_mode: bool = False,
        dry_run: bool = False,
    ) -> None:
        self.conn = conn
        self.env_file = env_file.resolve()
        self.profile_model = profile_model
        self.profile_version = profile_prompt_version(profile_model)
        self.summary_model = summary_model
        self._summary_generator: SubtopicSummaryGenerator | None = None
        self.json_mode = json_mode
        self.dry_run = dry_run
        self.engine = ValidatedSubtopicEngine(
            env_file=self.env_file,
            model=subtopic_model,
            json_mode=json_mode,
        )

    def bootstrap_existing(
        self,
        *,
        topic_id: int | None = None,
        event_id: int | None = None,
    ) -> dict[str, Any]:
        """Adopt the current DB snapshot without any LLM/Subtopic replay.

        This is the one-time bridge from the reviewed 17_78 file snapshot to
        the DB-backed runner.  Existing Profile content and Subtopic tables are
        never changed.  Cache metadata is backfilled for rows that already have
        a Profile, while current profile-less baseline Events are marked as
        processed exclusions only when they have no Subtopic membership.
        """

        repo.validate_schema(self.conn)
        events = repo.fetch_bootstrap_events(
            self.conn,
            topic_id=topic_id,
            event_id=event_id,
        )
        grouped: dict[int, list[EventRow]] = defaultdict(list)
        for event in events:
            grouped[event.topic_id].append(event)

        result: dict[str, Any] = {
            "bootstrap": True,
            "dry_run": self.dry_run,
            "scanned_event_count": len(events),
            "profile_metadata_updated_count": 0,
            "profileless_baseline_excluded_count": 0,
            "processed_marker_added_count": 0,
            "skipped_locked_topic_count": 0,
            "failures": [],
        }

        for current_topic_id in sorted(grouped):
            if not repo.try_topic_advisory_lock(self.conn, current_topic_id):
                result["skipped_locked_topic_count"] += 1
                continue
            try:
                for event in grouped[current_topic_id]:
                    try:
                        cached = repo.fetch_event_profile(self.conn, event.id)
                        if cached is None:
                            memberships = repo.event_membership_count(self.conn, event.id)
                            if memberships:
                                raise RuntimeError(
                                    "Profile이 없는데 기존 subtopic_events membership이 있습니다. "
                                    "자동 bootstrap에서 제외합니다."
                                )
                            prepared = self._prepare(event)
                            if not self.dry_run and event.subtopic_processed_at is None:
                                with self.conn.transaction():
                                    repo.lock_event(self.conn, event.id)
                                    self._assert_source_unchanged(prepared)
                                    repo.mark_events_processed(self.conn, [event.id])
                            if event.subtopic_processed_at is None:
                                result["processed_marker_added_count"] += 1
                            result["profileless_baseline_excluded_count"] += 1
                            continue

                        prepared = self._prepare(event)
                        event_summary = str(cached.get("event_summary") or "").strip()
                        anchors = cached.get("anchors")
                        if not event_summary or not isinstance(anchors, list):
                            raise RuntimeError("기존 event_profiles의 event_summary/anchors가 유효하지 않습니다.")

                        metadata_changed = (
                            cached.get("input_hash") != prepared.input_hash
                            or cached.get("prompt_version") != prepared.prompt_version
                        )
                        if not self.dry_run and (metadata_changed or event.subtopic_processed_at is None):
                            with self.conn.transaction():
                                repo.lock_event(self.conn, event.id)
                                self._assert_source_unchanged(prepared)
                                if metadata_changed:
                                    repo.adopt_event_profile_metadata(
                                        self.conn,
                                        event_id=event.id,
                                        input_hash=prepared.input_hash,
                                        prompt_version=prepared.prompt_version,
                                    )
                                if event.subtopic_processed_at is None:
                                    repo.mark_events_processed(self.conn, [event.id])
                        if metadata_changed:
                            result["profile_metadata_updated_count"] += 1
                        if event.subtopic_processed_at is None:
                            result["processed_marker_added_count"] += 1
                    except Exception as exc:
                        result["failures"].append(
                            {
                                "topic_id": current_topic_id,
                                "event_id": event.id,
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                            }
                        )
            finally:
                repo.release_topic_advisory_lock(self.conn, current_topic_id)

        if not self.dry_run:
            remaining = repo.fetch_pending_events(
                self.conn,
                prompt_version=self.profile_version,
                topic_id=topic_id,
                event_id=event_id,
                force=False,
            )
            result["remaining_pending_count"] = len(remaining)
        else:
            result["remaining_pending_count"] = None
        return result

    def run(
        self,
        *,
        topic_id: int | None = None,
        event_id: int | None = None,
        limit: int | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        repo.validate_schema(self.conn)
        scope_topic_id = topic_id
        if scope_topic_id is None and event_id is not None:
            scoped_event = repo.fetch_event(self.conn, event_id)
            scope_topic_id = scoped_event.topic_id if scoped_event else None
        runtime_profile_count = repo.count_unbootstrapped_profiles(
            self.conn,
            topic_id=scope_topic_id,
        )
        if runtime_profile_count:
            raise RuntimeError(
                f"기존 event_profiles {runtime_profile_count}건에 input_hash/prompt_version이 없습니다. "
                "일반 batch 전에 --bootstrap-existing을 먼저 실행하세요."
            )
        selected = repo.fetch_pending_events(
            self.conn,
            prompt_version=self.profile_version,
            topic_id=topic_id,
            event_id=event_id,
            force=force,
            limit=limit,
        )
        grouped: dict[int, list[EventRow]] = defaultdict(list)
        for event in selected:
            grouped[event.topic_id].append(event)

        result: dict[str, Any] = {
            "selected_event_count": len(selected),
            "processed_event_count": 0,
            "excluded_event_count": 0,
            "rebuilt_topic_count": 0,
            "skipped_locked_topic_count": 0,
            "summary_updated_count": 0,
            "summary_failures": [],
            "failures": [],
            "dry_run": self.dry_run,
        }

        summary_scope_topic_id = topic_id
        if summary_scope_topic_id is None and event_id is not None:
            target_event = repo.fetch_event(self.conn, event_id)
            summary_scope_topic_id = target_event.topic_id if target_event else None
        summary_topics = set(
            repo.fetch_topics_with_missing_summaries(
                self.conn,
                topic_id=summary_scope_topic_id,
            )
        )
        # With --event-id, do not unexpectedly retry summaries from unrelated
        # Topics if the Event disappeared or is not assigned.
        if event_id is not None and summary_scope_topic_id is None:
            summary_topics.clear()
        topic_ids = set(grouped) | summary_topics

        for current_topic_id in sorted(topic_ids):
            if not repo.try_topic_advisory_lock(self.conn, current_topic_id):
                logger.warning("topic_id=%s is already being processed; skipping", current_topic_id)
                result["skipped_locked_topic_count"] += 1
                continue
            try:
                if grouped.get(current_topic_id):
                    self._run_topic(
                        current_topic_id,
                        selected=grouped[current_topic_id],
                        force=force,
                        result=result,
                    )
                self._refresh_topic_summaries(current_topic_id, result)
            except Exception as exc:
                logger.exception("topic_id=%s Subtopic batch failed", current_topic_id)
                result["failures"].append(
                    {
                        "topic_id": current_topic_id,
                        **(
                            {"event_id": exc.event_id}
                            if isinstance(exc, EventProcessingError)
                            else {}
                        ),
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
            finally:
                repo.release_topic_advisory_lock(self.conn, current_topic_id)
        return result

    def _run_topic(
        self,
        topic_id: int,
        *,
        selected: list[EventRow],
        force: bool,
        result: dict[str, Any],
    ) -> None:
        selected_ids = {event.id for event in selected}
        refreshed = repo.fetch_pending_events(
            self.conn,
            prompt_version=self.profile_version,
            topic_id=topic_id,
            force=force,
        )
        if force:
            pending = [event for event in refreshed if event.id in selected_ids]
        else:
            # A targeted/limited invocation may select a later Event. Include all
            # older pending Events so the target cannot skip unseen past state.
            prepared_refreshed = [self._prepare(event) for event in refreshed]
            selected_prepared = [item for item in prepared_refreshed if item.event.id in selected_ids]
            if not selected_prepared:
                return
            cutoff = max(item.order_key for item in selected_prepared)
            pending = [item.event for item in prepared_refreshed if item.order_key <= cutoff]
        if not pending:
            return

        state, context = self._reconstruct_state(topic_id, pending[0].topic_name)
        prepared_pending = [self._prepare(event) for event in pending]
        prepared_pending.sort(key=lambda item: item.order_key)

        # events.updated_at can change for fields irrelevant to Subtopic input.
        # If the exact content-derived Profile input is unchanged, only advance
        # the completion marker instead of replaying the Topic.
        effective_pending: list[PreparedEvent] = []
        for prepared in prepared_pending:
            cached = repo.fetch_event_profile(self.conn, prepared.event.id)
            unchanged = from_cached_profile(prepared, cached) if cached else None
            if not force and prepared.event.subtopic_processed_at is not None and unchanged:
                self._ack_unchanged_event(prepared)
                result["processed_event_count"] += 1
            else:
                effective_pending.append(prepared)
        prepared_pending = effective_pending
        if not prepared_pending:
            return

        if force or self._requires_replay(state, prepared_pending):
            processed, excluded = self._rebuild_topic(topic_id, pending[0].topic_name, state)
            result["processed_event_count"] += processed
            result["excluded_event_count"] += excluded
            result["rebuilt_topic_count"] += 1
            return

        for prepared in prepared_pending:
            try:
                profile = self._profile(prepared)
                if prepared.article_span_days > MAX_ARTICLE_SPAN_DAYS:
                    excluded = state.setdefault("excluded_event_ids", [])
                    if str(prepared.event.id) not in excluded:
                        excluded.append(str(prepared.event.id))
                    self._persist_one(prepared, profile, state, remove_memberships=True)
                    result["processed_event_count"] += 1
                    result["excluded_event_count"] += 1
                    continue

                next_context = [*context, profile.runtime_profile]
                next_context.sort(key=self._profile_order_key)
                new_state = self.engine.process_one(
                    topic_name=prepared.event.topic_name,
                    state=state,
                    profiles=next_context,
                    current_event_id=prepared.event.id,
                )
                new_state["last_processed_order_key"] = list(prepared.order_key)
                self._persist_one(prepared, profile, new_state)
                state = new_state
                context = next_context
                result["processed_event_count"] += 1
            except EventProcessingError:
                raise
            except Exception as exc:
                raise EventProcessingError(prepared.event.id, str(exc)) from exc

    def _prepare(self, event: EventRow) -> PreparedEvent:
        try:
            return prepare_event(
                event,
                repo.fetch_event_articles(self.conn, event.id),
                prompt_version=self.profile_version,
            )
        except Exception as exc:
            if isinstance(exc, EventProcessingError):
                raise
            raise EventProcessingError(event.id, str(exc)) from exc

    def _ack_unchanged_event(self, prepared: PreparedEvent) -> None:
        if self.dry_run:
            logger.info("[dry-run] event_id=%s unchanged marker write skipped", prepared.event.id)
            return
        with self.conn.transaction():
            repo.lock_event(self.conn, prepared.event.id)
            self._assert_source_unchanged(prepared)
            repo.mark_events_processed(self.conn, [prepared.event.id])

    @staticmethod
    def _profile_order_key(item: dict[str, Any]) -> tuple[Any, ...]:
        raw_id = str(item["event_id"])
        event_id: int | str = int(raw_id) if raw_id.isdigit() else raw_id
        return (
            item["representative_published_date"],
            item["first_published_at"],
            event_id,
        )

    @staticmethod
    def _requires_replay(state: dict[str, Any], pending: list[PreparedEvent]) -> bool:
        if state.get("stale_processed_event_ids"):
            return True
        if any(item.event.subtopic_processed_at is not None for item in pending):
            return True
        processed_ids = {str(value) for value in state.get("processed_event_ids", [])}
        if any(str(item.event.id) in processed_ids for item in pending):
            return True
        last_key = state.get("last_processed_order_key")
        return last_key is not None and any(
            tuple(item.order_key) <= tuple(last_key) for item in pending
        )

    def _profile(self, prepared: PreparedEvent) -> ProfileResult:
        cached = repo.fetch_event_profile(self.conn, prepared.event.id)
        reusable = from_cached_profile(prepared, cached) if cached else None
        if reusable:
            return reusable
        generated = generate_profile(
            prepared,
            env_file=self.env_file,
            model=self.profile_model,
            json_mode=self.json_mode,
        )
        # Profile is a cache, not a completion marker. A later Subtopic failure
        # can reuse this successful call, but processed_at remains unchanged.
        if not self.dry_run:
            with self.conn.transaction():
                repo.lock_event(self.conn, prepared.event.id)
                self._assert_source_unchanged(prepared)
                # The Profile cache can commit before Subtopic classification.
                # Clear any summary that depends on the old Profile now so a
                # later classifier failure cannot leave stale user-facing text.
                repo.invalidate_event_subtopic_summaries(self.conn, prepared.event.id)
                repo.upsert_event_profile(self.conn, generated)
        return generated

    def _reconstruct_state(
        self,
        topic_id: int,
        topic_name: str,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Rebuild classifier state from the existing DB tables only."""

        state = initial_state(topic_name)
        state["stale_processed_event_ids"] = []
        eligible: list[tuple[PreparedEvent, ProfileResult]] = []
        for event in repo.fetch_topic_events(self.conn, topic_id):
            if event.subtopic_processed_at is None:
                continue
            cached = repo.fetch_event_profile(self.conn, event.id)
            # Bootstrap may intentionally adopt a historical Profile failure as
            # a processed baseline exclusion.  Such rows have no Profile and
            # must not force a full replay on the next new Event.
            if cached is None:
                state["excluded_event_ids"].append(str(event.id))
                continue
            prepared = self._prepare(event)
            if prepared.article_span_days > MAX_ARTICLE_SPAN_DAYS:
                state["excluded_event_ids"].append(str(event.id))
                continue
            profile = from_cached_profile(prepared, cached) if cached else None
            if profile is None:
                state["stale_processed_event_ids"].append(str(event.id))
                continue
            eligible.append((prepared, profile))

        eligible.sort(key=lambda pair: pair[0].order_key)
        eligible_ids = {str(item.event.id) for item, _ in eligible}
        state["processed_event_ids"] = [str(item.event.id) for item, _ in eligible]
        state["last_processed_step"] = len(eligible)
        if eligible:
            last = eligible[-1][0]
            state["last_processed_event_id"] = str(last.event.id)
            state["last_processed_order_key"] = list(last.order_key)

        mapping: dict[str, int] = {}
        max_number = 0
        for row in repo.fetch_topic_subtopics(self.conn, topic_id):
            db_id = int(row["id"])
            sid = f"ST{db_id:03d}"
            max_number = max(max_number, db_id)
            mapping[sid] = db_id
            state["subtopics"].append(
                {
                    "subtopic_id": sid,
                    "name": row["name"],
                    "display_name_status": "OK",
                    "type": row["type"],
                    "common_basis": row["common_basis"],
                    "membership_criterion": row["membership_criterion"],
                    "event_ids": [
                        str(value) for value in row["event_ids"] if str(value) in eligible_ids
                    ],
                    "creation_membership_validations": [],
                    "created_at_step": 0,
                    "created_by_event_id": None,
                    "candidate_reason": "reconstructed from DB",
                    "creation_scope_refinement": None,
                    "refinement_history": [],
                    "update_history": [],
                }
            )
        state["db_subtopic_ids"] = mapping
        state["next_subtopic_number"] = max_number + 1

        # No extra state table is introduced. Reconstruct the regular 7+10n
        # checkpoint; within this run the validated core carries exact state.
        step = len(eligible)
        if state["subtopics"] and step >= 7:
            state["last_consolidated_step"] = 7 + ((step - 7) // 10) * 10
            state["consolidation_count"] = max(0, (step - 7) // 10)

        context = [profile.runtime_profile for _, profile in eligible]
        context.sort(key=self._profile_order_key)
        return state, context

    def _assert_source_unchanged(self, prepared: PreparedEvent) -> None:
        current_event = repo.fetch_event(self.conn, prepared.event.id)
        if current_event is None:
            raise SourceChangedError(f"event_id={prepared.event.id} disappeared during processing")
        if current_event.topic_id != prepared.event.topic_id:
            raise SourceChangedError(
                f"event_id={prepared.event.id} topic_id changed during processing "
                f"({prepared.event.topic_id} -> {current_event.topic_id}); retry next batch"
            )
        current = self._prepare(current_event)
        if current.input_hash != prepared.input_hash:
            raise SourceChangedError(
                f"event_id={prepared.event.id} article input changed during processing; retry next batch"
            )

    def _persist_one(
        self,
        prepared: PreparedEvent,
        profile: ProfileResult,
        state: dict[str, Any],
        *,
        remove_memberships: bool = False,
    ) -> None:
        if self.dry_run:
            logger.info("[dry-run] event_id=%s DB writes skipped", prepared.event.id)
            return
        with self.conn.transaction():
            repo.lock_topic(self.conn, prepared.event.topic_id)
            repo.lock_event(self.conn, prepared.event.id)
            self._assert_source_unchanged(prepared)
            repo.invalidate_event_subtopic_summaries(self.conn, prepared.event.id)
            repo.upsert_event_profile(self.conn, profile)
            repo.remove_event_memberships(self.conn, prepared.event.id)
            if not remove_memberships:
                self._sync_public_state(prepared.event.topic_id, state)
            repo.mark_events_processed(self.conn, [prepared.event.id])

    @staticmethod
    def _semantic_key(item: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(item.get("type") or "").strip(),
            " ".join(str(item.get("common_basis") or "").split()).casefold(),
            " ".join(str(item.get("membership_criterion") or "").split()).casefold(),
        )

    def _sync_public_state(self, topic_id: int, state: dict[str, Any]) -> None:
        existing_rows = repo.fetch_topic_subtopics(self.conn, topic_id)
        existing_ids = {int(row["id"]) for row in existing_rows}
        reusable_by_key = {
            self._semantic_key(row): int(row["id"])
            for row in existing_rows
        }
        mapping = {
            str(key): int(value) for key, value in state.get("db_subtopic_ids", {}).items()
        }
        active_mapping: dict[str, int] = {}
        claimed_ids: set[int] = set()
        for item in state.get("subtopics", []):
            sid = str(item["subtopic_id"])
            db_id = mapping.get(sid)
            if db_id not in existing_ids or db_id in claimed_ids:
                db_id = reusable_by_key.get(self._semantic_key(item))
            if db_id is None or db_id in claimed_ids:
                db_id = repo.insert_subtopic(self.conn, topic_id, item)
            else:
                repo.update_subtopic(self.conn, db_id, topic_id, item)
            claimed_ids.add(db_id)
            active_mapping[sid] = db_id
            repo.sync_memberships(
                self.conn,
                topic_id=topic_id,
                db_subtopic_id=db_id,
                event_ids=(int(event_id) for event_id in item.get("event_ids", [])),
            )

        for db_id in sorted(existing_ids - claimed_ids):
            repo.delete_subtopic(self.conn, db_id, topic_id)
        state["db_subtopic_ids"] = active_mapping

    def _rebuild_topic(
        self,
        topic_id: int,
        topic_name: str,
        old_state: dict[str, Any],
    ) -> tuple[int, int]:
        """Chronologically replay a Topic when a past Event input changed."""

        logger.warning("topic_id=%s chronological full replay started", topic_id)
        prepared_events = [self._prepare(event) for event in repo.fetch_topic_events(self.conn, topic_id)]
        prepared_events.sort(key=lambda item: item.order_key)
        state = initial_state(topic_name)
        state["db_subtopic_ids"] = dict(old_state.get("db_subtopic_ids", {}))
        generated: list[tuple[PreparedEvent, ProfileResult]] = []
        context: list[dict[str, Any]] = []
        excluded_count = 0

        for prepared in prepared_events:
            try:
                profile = self._profile(prepared)
                generated.append((prepared, profile))
                if prepared.article_span_days > MAX_ARTICLE_SPAN_DAYS:
                    state["excluded_event_ids"].append(str(prepared.event.id))
                    excluded_count += 1
                    continue
                context.append(profile.runtime_profile)
                context.sort(key=self._profile_order_key)
                state = self.engine.process_one(
                    topic_name=topic_name,
                    state=state,
                    profiles=list(context),
                    current_event_id=prepared.event.id,
                )
                state["last_processed_order_key"] = list(prepared.order_key)
            except EventProcessingError:
                raise
            except Exception as exc:
                raise EventProcessingError(prepared.event.id, str(exc)) from exc

        if self.dry_run:
            logger.info("[dry-run] topic_id=%s replay DB writes skipped", topic_id)
            return len(prepared_events), excluded_count

        with self.conn.transaction():
            repo.lock_topic(self.conn, topic_id)
            for prepared, profile in generated:
                repo.lock_event(self.conn, prepared.event.id)
                self._assert_source_unchanged(prepared)
                repo.invalidate_event_subtopic_summaries(self.conn, prepared.event.id)
                repo.upsert_event_profile(self.conn, profile)
                repo.remove_event_memberships(self.conn, prepared.event.id)
            self._sync_public_state(topic_id, state)
            for event_id in state.get("excluded_event_ids", []):
                repo.remove_event_memberships(self.conn, int(event_id))
            repo.mark_events_processed(self.conn, [item.event.id for item in prepared_events])
        return len(prepared_events), excluded_count

    def _summary_generator_instance(self) -> SubtopicSummaryGenerator:
        if self._summary_generator is None:
            self._summary_generator = SubtopicSummaryGenerator(
                env_file=self.env_file,
                model=self.summary_model,
            )
        return self._summary_generator

    def _build_summary_input(self, topic_id: int, row: dict[str, Any]) -> dict[str, Any]:
        events: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        topic_name = ""
        for raw_event_id in row.get("event_ids", []):
            event_id = int(raw_event_id)
            event = repo.fetch_event(self.conn, event_id)
            if event is None or event.topic_id != topic_id:
                raise RuntimeError(
                    f"subtopic_id={row['id']}: event_id={event_id}를 같은 Topic에서 찾을 수 없습니다."
                )
            topic_name = event.topic_name
            cached = repo.fetch_event_profile(self.conn, event_id)
            if cached is None:
                raise RuntimeError(
                    f"subtopic_id={row['id']}: event_id={event_id}의 Event Profile이 없습니다."
                )
            event_summary = str(cached.get("event_summary") or "").strip()
            if not event_summary:
                raise RuntimeError(
                    f"subtopic_id={row['id']}: event_id={event_id}의 event_summary가 비어 있습니다."
                )
            prepared = self._prepare(event)
            events.append(
                (
                    prepared.order_key,
                    {
                        "event_id": str(event_id),
                        "date": prepared.profile_input["representative_published_date"],
                        "event_name": event.title,
                        "event_summary": event_summary,
                    },
                )
            )
        events.sort(key=lambda pair: pair[0])
        return {
            "topic_name": topic_name,
            "subtopic_id": str(row["id"]),
            "name": row.get("name") or "",
            "type": row.get("type") or "",
            "common_basis": row.get("common_basis") or "",
            "membership_criterion": row.get("membership_criterion") or "",
            "events": [item for _, item in events],
        }

    @staticmethod
    def _summary_input_fingerprint(item: dict[str, Any]) -> tuple[Any, ...]:
        return (
            item.get("name"),
            item.get("type"),
            item.get("common_basis"),
            tuple(
                (
                    event["event_id"],
                    event["date"],
                    event["event_name"],
                    event["event_summary"],
                )
                for event in item.get("events", [])
            ),
        )

    def _refresh_topic_summaries(self, topic_id: int, result: dict[str, Any]) -> None:
        rows = repo.fetch_subtopics_needing_summary(self.conn, topic_id)
        if not rows:
            return
        for row in rows:
            db_id = int(row["id"])
            try:
                item = self._build_summary_input(topic_id, row)
                expected = self._summary_input_fingerprint(item)
                summary = self._summary_generator_instance().generate(item)
                if self.dry_run:
                    logger.info("[dry-run] subtopic_id=%s summary write skipped", db_id)
                    result["summary_updated_count"] += 1
                    continue

                with self.conn.transaction():
                    current_rows = [
                        candidate
                        for candidate in repo.fetch_topic_subtopics(self.conn, topic_id)
                        if int(candidate["id"]) == db_id
                    ]
                    if not current_rows:
                        raise RuntimeError("summary 생성 중 Subtopic이 삭제되었습니다.")
                    current = current_rows[0]
                    if current.get("summary") is not None:
                        continue
                    current_item = self._build_summary_input(topic_id, current)
                    if self._summary_input_fingerprint(current_item) != expected:
                        raise RuntimeError("summary 생성 중 Subtopic 입력이 변경되었습니다. 다음 배치에서 재시도합니다.")
                    repo.update_subtopic_summary(
                        self.conn,
                        db_id=db_id,
                        topic_id=topic_id,
                        summary=summary,
                    )
                result["summary_updated_count"] += 1
            except Exception as exc:
                logger.exception("subtopic_id=%s summary generation failed", db_id)
                result["summary_failures"].append(
                    {
                        "topic_id": topic_id,
                        "subtopic_id": db_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
