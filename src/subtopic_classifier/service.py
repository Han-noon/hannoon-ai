from __future__ import annotations

import logging
from copy import deepcopy
from collections import defaultdict
from pathlib import Path
from typing import Any

from db import subtopics as repo

from .engine import ValidatedSubtopicEngine, initial_state
from .models import EventRow, PreparedEvent, ProfileResult
from .profile import from_cached_profile, generate_profile, prepare_event, profile_prompt_version
from .settings import MAX_ARTICLE_SPAN_DAYS, MIN_TOPIC_EVENTS
from .summary import SubtopicSummaryGenerator

logger = logging.getLogger(__name__)


class SourceChangedError(RuntimeError):
    """The Event/article input changed while an LLM request was running."""


class EventProcessingError(RuntimeError):
    def __init__(self, event_id: int, message: str) -> None:
        super().__init__(f"event_id={event_id}: {message}")
        self.event_id = event_id


class ClassifierStateUnavailableError(RuntimeError):
    """Exact 17_78 stream state cannot be reconstructed from public tables."""


class ProcessedProfileMissingError(RuntimeError):
    """A completed Event has no valid Profile and needs explicit recovery."""


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

    def run(
        self,
        *,
        topic_id: int | None = None,
        event_id: int | None = None,
        limit: int | None = None,
        replay_topic: bool = False,
    ) -> dict[str, Any]:
        repo.validate_schema(self.conn)
        if replay_topic:
            if topic_id is None or event_id is not None or limit is not None:
                raise ValueError("replay_topic requires topic_id and cannot use event_id/limit")
            selected = repo.fetch_topic_events(self.conn, topic_id)
        else:
            selected = repo.fetch_pending_events(
                self.conn,
                topic_id=topic_id,
                event_id=event_id,
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
            "skipped_below_threshold_topic_count": 0,
            "skipped_below_threshold_event_count": 0,
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
                        replay_topic=replay_topic,
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

    def import_checkpoint(self, topic_id: int, checkpoint: dict[str, Any]) -> dict[str, Any]:
        """Adopt an explicitly reconciled checkpoint without AI calls or public-data edits.

        The supplied IDs and active Subtopic data must already match the DB.
        Raw historical exports with local Event IDs or manual edits are not accepted.
        """
        repo.validate_schema(self.conn)
        if not repo.try_topic_advisory_lock(self.conn, topic_id):
            raise RuntimeError(f"topic_id={topic_id}: Topic is locked")
        try:
            with self.conn.transaction():
                repo.lock_topic(self.conn, topic_id)
                if repo.fetch_classifier_state(self.conn, topic_id) is not None:
                    raise ClassifierStateUnavailableError("기존 checkpoint는 import로 덮어쓸 수 없습니다")
                events = repo.fetch_topic_events(self.conn, topic_id)
                if not events:
                    raise ClassifierStateUnavailableError("Topic에 Event가 없습니다")
                completed = {str(e.id) for e in events if e.subtopic_processed_at is not None}
                state, context = self._validate_checkpoint(topic_id, checkpoint, completed,
                    repo.fetch_topic_subtopics(self.conn, topic_id))
                state["topic_name"] = events[0].topic_name
                if not self.dry_run:
                    repo.save_classifier_state(self.conn, topic_id, state, context)
            return {"topic_id": topic_id, "processed_event_count": len(completed),
                    "active_subtopic_count": len(state["subtopics"]), "dry_run": self.dry_run}
        finally:
            repo.release_topic_advisory_lock(self.conn, topic_id)

    def generate_profiles_only(
        self,
        *,
        topic_id: int | None = None,
        event_id: int | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Create missing/invalid Profiles without classifying or marking Events.

        This maintenance mode intentionally does not enforce the seven-Event
        Topic threshold.  A valid Profile already stored for an event_id is
        reused regardless of nullable cache metadata.
        """

        repo.validate_schema(self.conn, require_state=False)
        events = repo.fetch_profile_events(
            self.conn,
            topic_id=topic_id,
            event_id=event_id,
        )
        if limit is not None:
            events = events[:limit]
        result: dict[str, Any] = {
            "profiles_only": True,
            "selected_event_count": len(events),
            "generated_profile_count": 0,
            "reused_profile_count": 0,
            "failures": [],
            "dry_run": self.dry_run,
        }
        for event in events:
            locked = repo.try_topic_advisory_lock(self.conn, event.topic_id)
            if not locked:
                result["failures"].append({"topic_id": event.topic_id, "event_id": event.id,
                    "error_type": "TopicLocked", "error": "Topic is being classified; retry later"})
                continue
            try:
                current = repo.fetch_event(self.conn, event.id)
                if current is None or current.topic_id != event.topic_id:
                    raise SourceChangedError("Event Topic changed; retry later")
                prepared = self._prepare(current)
                profile = self._profile(prepared)
                key = "reused_profile_count" if profile.reused else "generated_profile_count"
                result[key] += 1
            except Exception as exc:
                result["failures"].append(
                    {
                        "topic_id": event.topic_id,
                        "event_id": event.id,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
            finally:
                repo.release_topic_advisory_lock(self.conn, event.topic_id)
        return result

    def _run_topic(
        self,
        topic_id: int,
        *,
        selected: list[EventRow],
        replay_topic: bool,
        result: dict[str, Any],
    ) -> None:
        all_topic_events = repo.fetch_topic_events(self.conn, topic_id)
        if len(all_topic_events) < MIN_TOPIC_EVENTS:
            result["skipped_below_threshold_topic_count"] += 1
            result["skipped_below_threshold_event_count"] += len(selected)
            return

        if replay_topic:
            processed, excluded = self._rebuild_topic(
                topic_id,
                all_topic_events[0].topic_name,
                initial_state(all_topic_events[0].topic_name),
            )
            result["processed_event_count"] += processed
            result["excluded_event_count"] += excluded
            result["rebuilt_topic_count"] += 1
            return

        # Once an eligible Topic is selected, consume all of its unprocessed
        # Events in chronological order.  A global --limit may choose which
        # Topics enter this run, but must not leave an exact in-memory stream
        # state half-finished.
        pending = repo.fetch_pending_events(self.conn, topic_id=topic_id)
        if not pending:
            return

        state, context = self._reconstruct_state(topic_id, pending[0].topic_name)
        prepared_pending = [self._prepare(event) for event in pending]
        prepared_pending.sort(key=lambda item: item.order_key)

        for prepared in prepared_pending:
            try:
                profile = self._profile(prepared)
                if prepared.article_span_days > MAX_ARTICLE_SPAN_DAYS:
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
                self._persist_one(prepared, new_state, next_context, previous_state=state)
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

    @staticmethod
    def _profile_order_key(item: dict[str, Any]) -> tuple[Any, ...]:
        return (
            item["representative_published_date"],
            item["first_published_at"],
            str(item["event_id"]),
        )

    def _profile(self, prepared: PreparedEvent, *, allow_processed_repair: bool = False) -> ProfileResult:
        cached = repo.fetch_event_profile(self.conn, prepared.event.id)
        reusable = from_cached_profile(prepared, cached) if cached else None
        if reusable:
            return reusable
        if prepared.event.subtopic_processed_at is not None and not allow_processed_repair:
            raise ProcessedProfileMissingError(
                f"event_id={prepared.event.id}: 처리 완료 Profile이 없거나 유효하지 않습니다. "
                "일반 Profile 생성으로 checkpoint를 변경할 수 없습니다. 명시적 replay 또는 이관이 필요합니다."
            )
        generated = generate_profile(
            prepared,
            env_file=self.env_file,
            model=self.profile_model,
            json_mode=self.json_mode,
        )
        # Profile is a cache, not a completion marker. A later Subtopic failure
        # can reuse this successful call, but processed_at remains unchanged.
        if not self.dry_run and not allow_processed_repair:
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
        """Restore the last committed checkpoint, never infer historical steps."""
        saved = repo.fetch_classifier_state(self.conn, topic_id)
        events = repo.fetch_topic_events(self.conn, topic_id)
        completed = {str(event.id) for event in events if event.subtopic_processed_at is not None}
        rows = repo.fetch_topic_subtopics(self.conn, topic_id)
        if saved is None:
            if completed or rows:
                raise ClassifierStateUnavailableError(
                    f"topic_id={topic_id}: 기존 분류 결과에 대응하는 checkpoint가 없습니다. "
                    "원본 state 검증/이관 또는 명시적 replay가 필요합니다."
                )
            return initial_state(topic_name), []
        state, context = self._validate_checkpoint(topic_id, saved, completed, rows)
        # A Topic title rename does not change its identity or checkpoint history.
        state["topic_name"] = topic_name
        return state, context

    def _validate_checkpoint(self, topic_id, saved, completed, rows):
        """Fail closed if public tables and the checkpoint have drifted."""
        def fail(message):
            raise ClassifierStateUnavailableError(f"topic_id={topic_id}: {message}")

        if saved.get("state_version") != repo.STATE_VERSION:
            fail("지원하지 않는 checkpoint 버전")
        state = deepcopy(saved.get("core_state"))
        context = deepcopy(saved.get("runtime_profiles"))
        if not isinstance(state, dict) or not isinstance(context, list):
            fail("checkpoint JSON 구조 오류")
        required = {"topic_name", "processed_event_ids", "subtopics", "next_subtopic_number",
                    "last_processed_event_id", "last_processed_step", "last_consolidated_step",
                    "consolidation_count", "retired_subtopics", "db_subtopic_ids"}
        if not required <= state.keys():
            fail("필수 state 필드 누락")
        ids = state["processed_event_ids"]
        if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids):
            fail("processed_event_ids 형식 오류")
        if len(ids) != len(set(ids)) or set(ids) != completed:
            fail("checkpoint와 DB 처리 완료 Event가 일치하지 않습니다")
        if (state["last_processed_step"] != len(ids) or
                state["last_processed_event_id"] != (ids[-1] if ids else None) or
                not isinstance(state["last_consolidated_step"], int) or
                not 0 <= state["last_consolidated_step"] <= len(ids) or
                not isinstance(state["consolidation_count"], int) or state["consolidation_count"] < 0):
            fail("checkpoint 처리 순서 또는 consolidation 이력 오류")
        profile_ids = [p.get("event_id") if isinstance(p, dict) else None for p in context]
        if len(profile_ids) != len(ids) or set(profile_ids) != completed:
            fail("checkpoint Profile과 완료 Event가 일치하지 않습니다")
        for profile in context:
            if not all(k in profile for k in ("event_name", "representative_published_date",
                                              "first_published_at", "event_summary", "anchors")):
                fail("checkpoint Profile 필수 정보 누락")
            cached = repo.fetch_event_profile(self.conn, int(profile["event_id"]))
            if (not cached or not str(cached.get("event_summary") or "").strip() or
                    not isinstance(cached.get("anchors"), list)):
                raise ProcessedProfileMissingError(f"event_id={profile['event_id']}: 유효한 DB Profile 없음")
            if (str(cached["event_summary"]).strip() != str(profile["event_summary"]).strip() or
                    cached["anchors"] != profile["anchors"]):
                fail(f"event_id={profile['event_id']}: DB Profile이 checkpoint와 달라졌습니다")
        mapping = state["db_subtopic_ids"]
        if not isinstance(mapping, dict) or not isinstance(state["subtopics"], list):
            fail("Subtopic 매핑 형식 오류")
        active = {str(item["subtopic_id"]): item for item in state["subtopics"]}
        db_rows = {int(row["id"]): row for row in rows}
        if (len(active) != len(state["subtopics"]) or set(active) != set(mapping) or
                len(set(mapping.values())) != len(mapping) or set(mapping.values()) != set(db_rows)):
            fail("checkpoint와 DB 활성 Subtopic ID가 일치하지 않습니다")
        for sid, item in active.items():
            row = db_rows[mapping[sid]]
            if any(item.get(k) != row.get(k) for k in ("name", "type", "common_basis", "membership_criterion")):
                fail(f"Subtopic {sid}: 분류 기준 또는 이름 불일치")
            members = [str(x) for x in item.get("event_ids", [])]
            db_members = [str(x) for x in row.get("event_ids", [])]
            if (len(members) != len(set(members)) or len(db_members) != len(set(db_members)) or
                    set(members) != set(db_members) or not set(members) <= completed):
                fail(f"Subtopic {sid}: membership 불일치")
        return state, context

    def _check_current_checkpoint(self, topic_id: int, previous_state: dict[str, Any]) -> None:
        # Recheck under the write transaction before applying the next snapshot.
        # The batch already holds the per-Topic advisory lock.
        events = repo.fetch_topic_events(self.conn, topic_id)
        if not events:
            raise SourceChangedError(f"topic_id={topic_id} disappeared")
        current, _ = self._reconstruct_state(topic_id, events[0].topic_name)
        expected = deepcopy(previous_state)
        expected["topic_name"] = events[0].topic_name
        if current != expected:
            raise SourceChangedError(f"topic_id={topic_id}: checkpoint changed during processing")

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
        state: dict[str, Any],
        context: list[dict[str, Any]],
        *,
        previous_state: dict[str, Any],
    ) -> None:
        if self.dry_run:
            logger.info("[dry-run] event_id=%s DB writes skipped", prepared.event.id)
            return
        with self.conn.transaction():
            repo.lock_topic(self.conn, prepared.event.topic_id)
            repo.lock_event(self.conn, prepared.event.id)
            self._assert_source_unchanged(prepared)
            topic_id = prepared.event.topic_id
            self._check_current_checkpoint(topic_id, previous_state)
            expected_ids = [*previous_state["processed_event_ids"], str(prepared.event.id)]
            if state.get("processed_event_ids") != expected_ids:
                raise RuntimeError("분류 코어가 예상과 다른 Event 처리 순서를 반환했습니다")
            self._sync_public_state(topic_id, state)
            self._validate_checkpoint(topic_id,
                {"state_version": repo.STATE_VERSION, "core_state": state, "runtime_profiles": context},
                set(expected_ids), repo.fetch_topic_subtopics(self.conn, topic_id))
            repo.save_classifier_state(self.conn, topic_id, state, context)
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
        excluded_ids: list[int] = []
        classified_ids: list[int] = []

        for prepared in prepared_events:
            try:
                profile = self._profile(prepared, allow_processed_repair=True)
                generated.append((prepared, profile))
                if prepared.article_span_days > MAX_ARTICLE_SPAN_DAYS:
                    state["excluded_event_ids"].append(str(prepared.event.id))
                    excluded_count += 1
                    excluded_ids.append(prepared.event.id)
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
                classified_ids.append(prepared.event.id)
            except EventProcessingError:
                raise
            except Exception as exc:
                raise EventProcessingError(prepared.event.id, str(exc)) from exc

        if self.dry_run:
            logger.info("[dry-run] topic_id=%s replay DB writes skipped", topic_id)
            return len(classified_ids), excluded_count

        with self.conn.transaction():
            repo.lock_topic(self.conn, topic_id)
            for prepared, profile in generated:
                repo.lock_event(self.conn, prepared.event.id)
                self._assert_source_unchanged(prepared)
                if not profile.reused:
                    repo.upsert_event_profile(self.conn, profile)
                repo.invalidate_event_subtopic_summaries(self.conn, prepared.event.id)
            self._sync_public_state(topic_id, state)
            self._validate_checkpoint(topic_id,
                {"state_version": repo.STATE_VERSION, "core_state": state, "runtime_profiles": context},
                {str(eid) for eid in classified_ids}, repo.fetch_topic_subtopics(self.conn, topic_id))
            repo.save_classifier_state(self.conn, topic_id, state, context)
            repo.clear_events_processed(self.conn, excluded_ids)
            repo.mark_events_processed(self.conn, classified_ids)
        return len(classified_ids), excluded_count

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
