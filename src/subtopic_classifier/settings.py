"""Shared constants and packaged assets for the validated Subtopic behavior."""

from __future__ import annotations

from pathlib import Path

MAX_ARTICLE_SPAN_DAYS = 30.0
DEFAULT_PROFILE_MODEL = "@preset/hannoon"
DEFAULT_SUBTOPIC_MODEL = "@preset/hannoon-subtopic"
DEFAULT_SUMMARY_MODEL = DEFAULT_SUBTOPIC_MODEL

PACKAGE_DIR = Path(__file__).resolve().parent
PROMPT_DIR = PACKAGE_DIR / "prompts"
SCHEMA_DIR = PACKAGE_DIR / "schemas"

PROFILE_RUNTIME_PATH = PACKAGE_DIR / "profile_runtime.py"
CLASSIFIER_RUNTIME_PATH = PACKAGE_DIR / "classifier_runtime.py"
RECOVERY_RUNTIME_PATH = PACKAGE_DIR / "recovery_runtime.py"

PROFILE_PROMPT_NAME = "17_77_event_profile_extraction_prompt.md"
PROFILE_SCHEMA_NAME = "17_77_event_profile_schema.json"
CANDIDATE_PROMPT_NAME = "17_77_subtopic_candidate_prompt.md"
CANDIDATE_SCHEMA_NAME = "17_77_subtopic_candidate_schema.json"
MEMBERSHIP_VALIDATOR_PROMPT_NAME = "17_77_membership_validator_prompt.md"
MEMBERSHIP_VALIDATOR_SCHEMA_NAME = "17_77_membership_validator_schema.json"
CONSOLIDATION_PLANNER_PROMPT_NAME = "17_77_consolidation_planner_prompt.md"
CONSOLIDATION_PLANNER_SCHEMA_NAME = "17_77_consolidation_planner_schema.json"
DUPLICATE_VALIDATOR_PROMPT_NAME = "17_77_duplicate_filter_validator_prompt.md"
DUPLICATE_VALIDATOR_SCHEMA_NAME = "17_77_duplicate_filter_validator_schema.json"
CONSOLIDATION_AUDIT_PROMPT_NAME = "17_77_consolidation_membership_audit_prompt.md"
CONSOLIDATION_AUDIT_SCHEMA_NAME = "17_77_consolidation_membership_audit_schema.json"
CONSOLIDATION_LABEL_PROMPT_NAME = "17_77_consolidation_label_prompt.md"
CONSOLIDATION_LABEL_SCHEMA_NAME = "17_77_consolidation_label_schema.json"
MISSING_DISCOVERY_PROMPT_NAME = "17_77_missing_subtopic_discovery_prompt.md"
MISSING_DISCOVERY_SCHEMA_NAME = "17_77_missing_subtopic_discovery_schema.json"
SUMMARY_PROMPT_NAME = "17_78_subtopic_summary_prompt.md"

_REQUIRED_RUNTIME_PATHS = (
    PROFILE_RUNTIME_PATH,
    CLASSIFIER_RUNTIME_PATH,
    RECOVERY_RUNTIME_PATH,
)
_REQUIRED_PROMPTS = (
    PROFILE_PROMPT_NAME,
    CANDIDATE_PROMPT_NAME,
    MEMBERSHIP_VALIDATOR_PROMPT_NAME,
    CONSOLIDATION_PLANNER_PROMPT_NAME,
    DUPLICATE_VALIDATOR_PROMPT_NAME,
    CONSOLIDATION_AUDIT_PROMPT_NAME,
    CONSOLIDATION_LABEL_PROMPT_NAME,
    MISSING_DISCOVERY_PROMPT_NAME,
    SUMMARY_PROMPT_NAME,
)
_REQUIRED_SCHEMAS = (
    PROFILE_SCHEMA_NAME,
    CANDIDATE_SCHEMA_NAME,
    MEMBERSHIP_VALIDATOR_SCHEMA_NAME,
    CONSOLIDATION_PLANNER_SCHEMA_NAME,
    DUPLICATE_VALIDATOR_SCHEMA_NAME,
    CONSOLIDATION_AUDIT_SCHEMA_NAME,
    CONSOLIDATION_LABEL_SCHEMA_NAME,
    MISSING_DISCOVERY_SCHEMA_NAME,
)


def prompt_path(name: str) -> Path:
    return PROMPT_DIR / name


def schema_path(name: str) -> Path:
    return SCHEMA_DIR / name


def validate_runtime_assets() -> None:
    """Fail early when a required packaged runtime asset is missing."""

    required = list(_REQUIRED_RUNTIME_PATHS)
    required += [prompt_path(name) for name in _REQUIRED_PROMPTS]
    required += [schema_path(name) for name in _REQUIRED_SCHEMAS]
    missing = [str(path.relative_to(PACKAGE_DIR)) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(
            "hannoon-ai Subtopic runtime assets are incomplete: " + ", ".join(missing)
        )
