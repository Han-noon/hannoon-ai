from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .profile import pipeline_child_env
from .settings import (
    CANDIDATE_PROMPT_NAME,
    CANDIDATE_SCHEMA_NAME,
    CLASSIFIER_RUNTIME_PATH,
    CONSOLIDATION_AUDIT_PROMPT_NAME,
    CONSOLIDATION_AUDIT_SCHEMA_NAME,
    CONSOLIDATION_LABEL_PROMPT_NAME,
    CONSOLIDATION_LABEL_SCHEMA_NAME,
    CONSOLIDATION_PLANNER_PROMPT_NAME,
    CONSOLIDATION_PLANNER_SCHEMA_NAME,
    DUPLICATE_VALIDATOR_PROMPT_NAME,
    DUPLICATE_VALIDATOR_SCHEMA_NAME,
    MEMBERSHIP_VALIDATOR_PROMPT_NAME,
    MEMBERSHIP_VALIDATOR_SCHEMA_NAME,
    MISSING_DISCOVERY_PROMPT_NAME,
    MISSING_DISCOVERY_SCHEMA_NAME,
    RECOVERY_RUNTIME_PATH,
    prompt_path,
    schema_path,
    validate_runtime_assets,
)


class EngineExecutionError(RuntimeError):
    pass


def initial_state(topic_name: str) -> dict[str, Any]:
    return {
        "topic_name": topic_name,
        "processed_event_ids": [],
        "subtopics": [],
        "next_subtopic_number": 1,
        "last_processed_event_id": None,
        "last_processed_step": 0,
        "last_consolidated_step": 0,
        "consolidation_count": 0,
        "retired_subtopics": [],
        "db_subtopic_ids": {},
        "excluded_event_ids": [],
    }


class ValidatedSubtopicEngine:
    """Runs the validated Subtopic runtime against ephemeral files.

    The production package contains only the runtime modules, prompts and schemas
    required by this service. Temporary JSON/JSONL files are deleted after each call.
    """

    def __init__(
        self,
        *,
        env_file: Path,
        model: str | None = None,
        json_mode: bool = False,
        max_candidates: int = 3,
    ) -> None:
        self.env_file = env_file
        self.model = model
        self.json_mode = json_mode
        self.max_candidates = max_candidates

    @staticmethod
    def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )

    @staticmethod
    def _run(cmd: list[str], label: str) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=pipeline_child_env(),
        )
        if completed.returncode != 0:
            detail = (completed.stdout + "\n" + completed.stderr).strip()[-8000:]
            raise EngineExecutionError(f"{label} 실패\n{detail}")
        return completed

    def process_one(
        self,
        *,
        topic_name: str,
        state: dict[str, Any],
        profiles: list[dict[str, Any]],
        current_event_id: int,
    ) -> dict[str, Any]:
        validate_runtime_assets()
        expected = str(current_event_id)
        if expected in {str(value) for value in state.get("processed_event_ids", [])}:
            raise EngineExecutionError(f"event_id={current_event_id}는 이미 state에서 처리됐습니다.")

        with tempfile.TemporaryDirectory(prefix="hannoon-subtopic-") as temp_name:
            root = Path(temp_name)
            stream = root / "stream"
            stream.mkdir()
            profiles_path = root / "profiles.jsonl"
            state_path = stream / "subtopic_state.json"
            self._write_jsonl(profiles_path, profiles)
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

            consolidation_before = int(state.get("consolidation_count", 0))
            cmd = [
                sys.executable,
                str(CLASSIFIER_RUNTIME_PATH),
                "--input", str(profiles_path),
                "--topic-name", topic_name,
                "--output-dir", str(stream),
                "--limit", "1",
                "--env-file", str(self.env_file),
                "--candidate-prompt", str(prompt_path(CANDIDATE_PROMPT_NAME)),
                "--validator-prompt", str(prompt_path(MEMBERSHIP_VALIDATOR_PROMPT_NAME)),
                "--candidate-schema", str(schema_path(CANDIDATE_SCHEMA_NAME)),
                "--validator-schema", str(schema_path(MEMBERSHIP_VALIDATOR_SCHEMA_NAME)),
                "--consolidation-planner-prompt", str(prompt_path(CONSOLIDATION_PLANNER_PROMPT_NAME)),
                "--duplicate-validator-prompt", str(prompt_path(DUPLICATE_VALIDATOR_PROMPT_NAME)),
                "--consolidation-audit-prompt", str(prompt_path(CONSOLIDATION_AUDIT_PROMPT_NAME)),
                "--consolidation-label-prompt", str(prompt_path(CONSOLIDATION_LABEL_PROMPT_NAME)),
                "--consolidation-planner-schema", str(schema_path(CONSOLIDATION_PLANNER_SCHEMA_NAME)),
                "--duplicate-validator-schema", str(schema_path(DUPLICATE_VALIDATOR_SCHEMA_NAME)),
                "--consolidation-audit-schema", str(schema_path(CONSOLIDATION_AUDIT_SCHEMA_NAME)),
                "--consolidation-label-schema", str(schema_path(CONSOLIDATION_LABEL_SCHEMA_NAME)),
                "--stop-on-event-error",
                "--stop-on-consolidation-error",
            ]
            if self.model:
                cmd += ["--model", self.model]
            if self.json_mode:
                cmd.append("--json-mode")
            self._run(cmd, f"event_id={current_event_id} Subtopic core")

            result = json.loads(state_path.read_text(encoding="utf-8"))
            if str(result.get("last_processed_event_id")) != expected:
                raise EngineExecutionError(
                    f"예상 Event({expected})가 아니라 {result.get('last_processed_event_id')}가 처리됐습니다."
                )

            if int(result.get("consolidation_count", 0)) > consolidation_before:
                log_path = stream / "consolidation_runs.jsonl"
                logs = [
                    json.loads(line)
                    for line in log_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ] if log_path.exists() else []
                if not logs or not str(logs[-1].get("status", "")).startswith("SUCCESS"):
                    status = logs[-1].get("status") if logs else "MISSING_LOG"
                    raise EngineExecutionError(f"Consolidation이 성공하지 않았습니다: {status}")
                checkpoint = int(logs[-1].get("checkpoint_step", result["last_processed_step"]))
                recovery_dir = root / f"recovery_{checkpoint:03d}"
                recovery_cmd = [
                    sys.executable,
                    str(RECOVERY_RUNTIME_PATH),
                    "--profiles", str(profiles_path),
                    "--baseline-state", str(state_path),
                    "--topic-name", topic_name,
                    "--output-dir", str(recovery_dir),
                    "--discovery-prompt", str(prompt_path(MISSING_DISCOVERY_PROMPT_NAME)),
                    "--discovery-schema", str(schema_path(MISSING_DISCOVERY_SCHEMA_NAME)),
                    "--validator-prompt", str(prompt_path(MEMBERSHIP_VALIDATOR_PROMPT_NAME)),
                    "--validator-schema", str(schema_path(MEMBERSHIP_VALIDATOR_SCHEMA_NAME)),
                    "--env-file", str(self.env_file),
                    "--max-candidates", str(self.max_candidates),
                    "--reset",
                ]
                if self.model:
                    recovery_cmd += ["--model", self.model]
                if self.json_mode:
                    recovery_cmd.append("--json-mode")
                self._run(recovery_cmd, f"checkpoint={checkpoint} Missing Recovery")
                recovered = recovery_dir / "17_77_subtopic_state_recovered.json"
                if not recovered.exists():
                    raise EngineExecutionError("Missing Recovery 결과 state가 없습니다.")
                result = json.loads(recovered.read_text(encoding="utf-8"))

            # The validated runtime deep-copies unknown top-level fields, but keep these
            # service fields explicit for old states and defensive compatibility.
            result.setdefault("db_subtopic_ids", dict(state.get("db_subtopic_ids", {})))
            result.setdefault("excluded_event_ids", list(state.get("excluded_event_ids", [])))
            return result
