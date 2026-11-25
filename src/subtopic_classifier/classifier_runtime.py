import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from openai import OpenAI


DEFAULT_MODEL = "@preset/hannoon-subtopic"

ALLOWED_SUBTOPIC_TYPES = {
    "FOCAL_POINT",
    "PROCESS",
    "RECURRING_ISSUE",
}


# ---------------------------------------------------------
# Basic I/O
# ---------------------------------------------------------

def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_jsonl(path: Path) -> list[dict]:
    rows = []

    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()

            if not line:
                continue

            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"{path} {line_no}번째 줄 JSON 파싱 실패: {e}"
                ) from e

            rows.append(row)

    return rows


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def save_json_atomic(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    temp = path.with_suffix(path.suffix + ".tmp")

    temp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    temp.replace(path)


# ---------------------------------------------------------
# Sorting / parsing
# ---------------------------------------------------------

def parse_sort_datetime(value: str) -> tuple:
    value = value or ""

    try:
        dt = datetime.fromisoformat(value)
        return (0, dt.isoformat())
    except Exception:
        return (1, value)


def event_sort_key(event: dict) -> tuple:
    return (
        event.get("representative_published_date") or "",
        parse_sort_datetime(event.get("first_published_at") or ""),
        event.get("event_id") or "",
    )


def extract_text_content(message_content: Any) -> str:
    if isinstance(message_content, str):
        return message_content

    if isinstance(message_content, list):
        parts = []

        for item in message_content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")

                if text:
                    parts.append(str(text))
            else:
                text = getattr(item, "text", None)

                if text:
                    parts.append(str(text))

        return "\n".join(parts)

    return str(message_content)


def strip_code_fence(text: str) -> str:
    text = text.strip()

    if text.startswith("```"):
        text = re.sub(
            r"^```(?:json)?\s*",
            "",
            text,
            count=1,
            flags=re.I,
        )
        text = re.sub(
            r"\s*```$",
            "",
            text,
            count=1,
        )

    return text.strip()


def parse_json_response(text: str) -> dict:
    cleaned = strip_code_fence(text)

    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")

        if start == -1 or end == -1 or end <= start:
            raise

        obj = json.loads(cleaned[start:end + 1])

    if not isinstance(obj, dict):
        raise ValueError("응답 JSON 최상위가 객체가 아닙니다.")

    return obj


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


# ---------------------------------------------------------
# State
# ---------------------------------------------------------

def get_processed_event_ids(state: dict) -> set[str]:
    return set(state.get("processed_event_ids", []))


def get_existing_subtopic_ids(state: dict) -> set[str]:
    return {
        item["subtopic_id"]
        for item in state.get("subtopics", [])
    }


def next_subtopic_id(state: dict) -> str:
    return f"ST{state.get('next_subtopic_number', 1):03d}"


def initial_state(topic_name: str) -> dict:
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
    }


def ensure_consolidation_state_fields(state: dict) -> dict:
    state.setdefault("last_consolidated_step", 0)
    state.setdefault("consolidation_count", 0)
    state.setdefault("retired_subtopics", [])
    return state


def reset_output_dir(output_dir: Path) -> None:
    if output_dir.exists():
        shutil.rmtree(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)


def event_memberships(state: dict, event_id: str) -> list[str]:
    result = []

    for subtopic in state.get("subtopics", []):
        if event_id in subtopic.get("event_ids", []):
            result.append(subtopic["subtopic_id"])

    return result


# ---------------------------------------------------------
# Summary sentence indexing
# ---------------------------------------------------------

def split_summary_sentences(text: str) -> list[str]:
    """
    Deterministically split event_summary into evidence units.

    We intentionally keep punctuation on each sentence and avoid any
    model-generated quoting/paraphrasing. The validator only selects an index.
    """
    text = re.sub(r"\s+", " ", (text or "").strip())

    if not text:
        return []

    # Split after common Korean/English sentence-ending punctuation.
    parts = re.split(r"(?<=[.!?])\s+", text)
    parts = [part.strip() for part in parts if part.strip()]

    # If no punctuation-based split was possible, use the whole summary.
    return parts or [text]


def build_summary_sentence_items(event_profile: dict) -> list[dict]:
    return [
        {
            "index": idx,
            "text": sentence,
        }
        for idx, sentence in enumerate(
            split_summary_sentences(
                event_profile.get("event_summary") or ""
            )
        )
    ]


# ---------------------------------------------------------
# Payload builders
# ---------------------------------------------------------

def build_candidate_payload(
    *,
    topic_name: str,
    current_event: dict,
    past_events: list[dict],
    state: dict,
) -> str:

    # fast1:
    # Candidate 단계는 "발견"만 담당한다.
    # 과거 Event의 summary/anchors 전체를 보내지 않고
    # Event core인 event_name 중심의 간소화 인덱스만 보낸다.
    compact_past_events = [
        {
            "event_id": event["event_id"],
            "event_name": event["event_name"],
            "representative_published_date": event[
                "representative_published_date"
            ],
        }
        for event in past_events
    ]

    existing_subtopics = []

    for subtopic in state.get("subtopics", []):
        existing_subtopics.append({
            "subtopic_id": subtopic["subtopic_id"],
            "name": subtopic["name"],
            "type": subtopic["type"],
            "common_basis": subtopic["common_basis"],
            "membership_criterion": subtopic["membership_criterion"],
        })

    payload = {
        "topic_name": topic_name,
        "current_event": current_event,
        "past_events": compact_past_events,
        "existing_subtopics": existing_subtopics,
    }

    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_validator_payload(
    *,
    topic_name: str,
    subtopic: dict,
    event_profile: dict,
) -> str:

    validator_event_profile = dict(event_profile)
    validator_event_profile["summary_sentences"] = (
        build_summary_sentence_items(event_profile)
    )
    # periodic7: DIRECT 판정의 유일한 근거.
    validator_event_profile["direct_evidence_text"] = (
        event_profile["event_name"]
    )

    payload = {
        "topic_name": topic_name,
        "subtopic": {
            "name": subtopic["name"],
            "type": subtopic["type"],
            "common_basis": subtopic["common_basis"],
            "membership_criterion": subtopic["membership_criterion"],
        },
        "event_profile": validator_event_profile,
    }

    return json.dumps(payload, ensure_ascii=False, indent=2)


def collect_member_membership_evidence(
    subtopic: dict,
    *,
    pending_validations: list[dict] | None = None,
) -> list[dict]:

    by_event_id = {}

    for item in subtopic.get("creation_membership_validations", []):
        event_id = item.get("event_id")

        if event_id:
            by_event_id[event_id] = {
                "event_id": event_id,
                "relation": item.get("relation"),
                "related_path": item.get("related_path"),
                "evidence_scope": item.get("evidence_scope"),
            }

    for history in subtopic.get("update_history", []):
        for item in history.get("membership_validations", []):
            event_id = item.get("event_id")

            if event_id:
                by_event_id[event_id] = {
                    "event_id": event_id,
                    "relation": item.get("relation"),
                    "related_path": item.get("related_path"),
                    "evidence_scope": item.get("evidence_scope"),
                }

    for item in pending_validations or []:
        event_id = item.get("event_id")

        if event_id:
            by_event_id[event_id] = {
                "event_id": event_id,
                "relation": item.get("relation"),
                "related_path": item.get("related_path"),
                "evidence_scope": item.get("evidence_scope"),
            }

    return [
        by_event_id[event_id]
        for event_id in subtopic.get("event_ids", [])
        if event_id in by_event_id
    ]


def build_refiner_payload(
    *,
    topic_name: str,
    subtopic: dict,
    member_events: list[dict],
    member_membership_evidence: list[dict],
) -> str:

    compact_member_events = []

    for event in member_events:
        compact_member_events.append({
            "event_id": event["event_id"],
            "event_name": event["event_name"],
            "representative_published_date": event["representative_published_date"],
            "event_summary": event["event_summary"],
            "anchors": event.get("anchors", []),
        })

    payload = {
        "topic_name": topic_name,
        "subtopic": {
            "subtopic_id": subtopic["subtopic_id"],
            "name": subtopic["name"],
            "type": subtopic["type"],
            "common_basis": subtopic["common_basis"],
            "membership_criterion": subtopic["membership_criterion"],
            "event_ids": list(subtopic["event_ids"]),
        },
        "member_events": compact_member_events,
        "member_membership_evidence": member_membership_evidence,
    }

    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------
# Model call
# ---------------------------------------------------------

def call_model(
    *,
    client: OpenAI,
    model: str,
    prompt: str,
    payload: str,
    temperature: float,
    max_tokens: int,
    json_mode: bool,
    validation_feedback: str | None = None,
    previous_bad_output: str | None = None,
):

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": payload},
    ]

    if validation_feedback:
        if previous_bad_output:
            messages.append({
                "role": "assistant",
                "content": previous_bad_output[-6000:],
            })

        messages.append({
            "role": "user",
            "content": (
                "직전 응답이 JSON 문법 또는 검증 규칙을 위반했습니다. "
                "위의 직전 응답을 그대로 복사하지 말고, "
                "입력 데이터는 유지한 채 오류를 수정하여 "
                "유효한 JSON 객체 하나만 다시 반환하세요.\n\n"
                f"[검증 오류]\n{validation_feedback}"
            ),
        })

    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    return client.chat.completions.create(**kwargs)


def usage_dict(response) -> dict:
    usage = getattr(response, "usage", None)

    if not usage:
        return {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        }

    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


# ---------------------------------------------------------
# Candidate validation
# ---------------------------------------------------------

def validate_event_local_membership_criterion(
    criterion: str,
    *,
    label: str,
) -> str:
    criterion = re.sub(r"\s+", " ", criterion.strip())

    criterion_cf = criterion.casefold()

    forbidden = [
        "여러 이벤트",
        "여러 event",
        "복수 이벤트",
        "둘 이상의 이벤트",
        "이벤트들에서",
        "이벤트들에",
        "여러 사건",
        "공통적으로 제시",
        "공통적으로 등장",
        "반복적으로 제시",
        "반복적으로 등장",
        "전체 흐름에서 반복",
    ]

    for phrase in forbidden:
        if phrase.casefold() in criterion_cf:
            raise ValueError(
                f"{label}.membership_criterion은 Event 하나만 보고 "
                f"판정할 수 있어야 합니다. 클러스터 전체 표현 금지: {phrase}"
            )

    if (
        "이 event" not in criterion_cf
        and "이 이벤트" not in criterion_cf
    ):
        raise ValueError(
            f"{label}.membership_criterion은 반드시 '이 Event' 또는 "
            "'이 이벤트'를 주어로 사용해 Event 하나의 귀속 조건으로 작성하세요."
        )

    return criterion



# ---------------------------------------------------------
# Subtopic display-name rules
# ---------------------------------------------------------

NAME_BANNED_TOKENS = {"및", "그리고", "또는", "혹은"}
NAME_BANNED_CHARS = {",", "/", "&", "+", "|", ";"}
NAME_SUMMARY_SUFFIXES = (
    "합의", "결렬", "요구", "촉구", "발표", "체결", "부결",
    "가결", "통과", "착수", "재개", "일정", "논의", "검토",
    "추궁",
)


def _has_non_numeric_middle_dot(name: str) -> bool:
    for i, char in enumerate(name):
        if char != "·":
            continue
        left = name[i - 1] if i > 0 else ""
        right = name[i + 1] if i + 1 < len(name) else ""
        if not (left.isdigit() and right.isdigit()):
            return True
    return False


def _name_has_korean_conjunction(name: str) -> bool:
    tokens = name.split()
    for token in tokens[:-1]:
        cleaned = token.strip("()[]{}\"'")
        if (
            len(cleaned) >= 3
            and cleaned[-1] in {"와", "과"}
            and len(cleaned[:-1]) >= 2
        ):
            return True
    return False


def subtopic_name_violations(name: Any) -> list[str]:
    if not isinstance(name, str):
        return ["NOT_STRING"]

    normalized = re.sub(r"\s+", " ", name.strip())

    if not normalized:
        return ["EMPTY"]

    issues = []
    words = normalized.split()

    if len(words) > 6:
        issues.append("TOO_LONG")

    for token in NAME_BANNED_TOKENS:
        if token in words:
            issues.append(f"BANNED_TOKEN:{token}")

    for char in NAME_BANNED_CHARS:
        if char in normalized:
            issues.append(f"BANNED_CHAR:{char}")

    if _name_has_korean_conjunction(normalized):
        issues.append("KOREAN_CONJUNCTION_WA_GWA")

    if normalized.endswith((".", ":", "!", "?")):
        issues.append("SENTENCE_PUNCTUATION")

    return issues


def subtopic_filter_name_violations(name: Any) -> list[str]:
    # periodic5: Python은 문법적/길이 문제만 검사한다.
    # 상태·결과형 이름 여부는 Label 모델의 의미 판단에 맡긴다.
    return subtopic_name_violations(name)



def strip_summary_suffix(name: str) -> str:
    value = re.sub(
        r"\s+",
        " ",
        (name or "").strip(),
    )

    changed = True

    while changed and value:
        changed = False

        for suffix in NAME_SUMMARY_SUFFIXES:
            marker = " " + suffix

            if value.endswith(marker):
                value = value[
                    :-len(marker)
                ].strip()
                changed = True
                break

    return value


def simplify_subtopic_name(name: Any) -> str:
    """
    병렬 나열/결과 꼬리표만 제거한다.
    common_basis exact-substring 여부는 검사하지 않는다.
    """
    if not isinstance(name, str):
        return ""

    value = re.sub(
        r"\s+",
        " ",
        name.strip(),
    )

    if not value:
        return ""

    # 숫자 사이 · 는 날짜/연도 표기로 보존.
    split_patterns = [
        r"\s+및\s+",
        r"\s+그리고\s+",
        r"\s+또는\s+",
        r"\s+혹은\s+",
        r"\s*,\s*",
        r"\s*/\s*",
        r"\s*&\s*",
        r"\s*\+\s*",
        r"\s*\|\s*",
        r"\s*;\s*",
    ]

    earliest = None

    for pattern in split_patterns:
        match = re.search(
            pattern,
            value,
        )

        if match and (
            earliest is None
            or match.start() < earliest
        ):
            earliest = match.start()

    if earliest is not None:
        value = value[:earliest].strip()

    tokens = value.split()
    rebuilt = []

    for token in tokens:
        cleaned = token.strip()

        if (
            len(cleaned) >= 3
            and cleaned[-1] in {"와", "과"}
            and len(cleaned[:-1]) >= 2
        ):
            rebuilt.append(
                cleaned[:-1]
            )
            break

        rebuilt.append(cleaned)

    value = " ".join(rebuilt).strip()

    words = value.split()

    if len(words) > 6:
        value = " ".join(words[:6])

    value = value.strip(
        " ,/&+|;:.!?"
    )

    return re.sub(
        r"\s+",
        " ",
        value,
    ).strip()


def _temporary_name_from_basis(
    common_basis: Any,
) -> str:
    """
    이름 생성 실패가 Subtopic 후보 탈락으로 이어지지 않게 하는
    마지막 display-only fallback.
    """
    basis = (
        common_basis
        if isinstance(common_basis, str)
        else ""
    )

    basis = re.sub(
        r"\s+",
        " ",
        basis.strip(),
    )

    if not basis:
        return "세부 관점"

    # 첫 절을 우선 사용.
    first_clause = re.split(
        r"[.!?]|(?:\s+-\s+)",
        basis,
        maxsplit=1,
    )[0].strip()

    candidate = simplify_subtopic_name(
        first_clause
    )

    if (
        candidate
        and not subtopic_name_violations(
            candidate
        )
    ):
        return candidate

    words = first_clause.split()

    if words:
        fallback = " ".join(
            words[:6]
        ).strip(" ,/&+|;:.!?")

        if fallback:
            return fallback

    return "세부 관점"


def repair_display_name(
    proposed: Any,
    *,
    common_basis: str,
    fallback_names: list[Any] | None = None,
) -> tuple[str, str, list[str]]:
    """
    이름은 display-only다.

    반환:
    - name
    - status: OK / REPAIRED / FALLBACK
    - warnings

    절대로 빈 이름 때문에 Subtopic 후보 자체를 버리지 않는다.
    """
    warnings = []
    fallback_names = (
        fallback_names or []
    )

    candidates = [
        proposed,
        *fallback_names,
    ]

    # 1) 이미 규칙을 만족하는 이름.
    for index, candidate in enumerate(
        candidates
    ):
        if not isinstance(
            candidate,
            str,
        ):
            continue

        candidate = re.sub(
            r"\s+",
            " ",
            candidate.strip(),
        )

        if not candidate:
            continue

        if not subtopic_filter_name_violations(
            candidate
        ):
            if index == 0:
                return (
                    candidate,
                    "OK",
                    warnings,
                )

            warnings.append(
                "기존/보조 display name fallback 사용"
            )

            return (
                candidate,
                "FALLBACK",
                warnings,
            )

    # 2) 제안 이름부터 보수적으로 문법만 보정.
    for candidate in candidates:
        simplified = simplify_subtopic_name(
            candidate
        )

        if (
            simplified
            and not subtopic_filter_name_violations(
                simplified
            )
        ):
            warnings.append(
                "display name 규칙만 자동 보정"
            )

            return (
                simplified,
                "REPAIRED",
                warnings,
            )

    # 3) basis에서 임시 display name 생성.
    temporary = _temporary_name_from_basis(
        common_basis
    )

    warnings.append(
        "display name 생성 실패 -> common_basis 기반 임시 이름 사용"
    )

    return (
        temporary,
        "FALLBACK",
        warnings,
    )


def validate_candidate_output(
    raw: dict,
    *,
    current_event_id: str,
    available_event_ids: set[str],
    state: dict,
) -> dict:
    if raw.get("current_event_id") != current_event_id:
        raise ValueError("candidate.current_event_id가 입력 current_event와 다릅니다.")

    existing_candidates = raw.get("existing_membership_candidates")
    new_candidates = raw.get("new_subtopic_candidates")

    if not isinstance(existing_candidates, list):
        raise ValueError("existing_membership_candidates가 배열이 아닙니다.")
    if not isinstance(new_candidates, list):
        raise ValueError("new_subtopic_candidates가 배열이 아닙니다.")

    notes = []
    existing_by_id = {
        item["subtopic_id"]: item
        for item in state.get("subtopics", [])
    }

    seen_existing_ids = set()
    normalized_existing = []

    for i, item in enumerate(existing_candidates, start=1):
        if not isinstance(item, dict):
            notes.append(f"existing[{i}] DROP: 객체가 아님")
            continue
        sid = item.get("subtopic_id")
        if sid not in existing_by_id:
            notes.append(f"existing[{i}] DROP: 존재하지 않는 subtopic_id={sid}")
            continue
        if sid in seen_existing_ids:
            notes.append(f"existing[{i}] DROP: 중복 subtopic_id={sid}")
            continue
        seen_existing_ids.add(sid)

        ids = item.get("candidate_event_ids")
        if not isinstance(ids, list):
            notes.append(f"{sid} DROP: candidate_event_ids가 배열이 아님")
            continue
        ids = list(dict.fromkeys(ids))
        invalid = [eid for eid in ids if eid not in available_event_ids]
        if invalid:
            notes.append(f"{sid}: 존재하지 않는 Event 제거=" + ",".join(map(str, invalid)))

        if current_event_id not in ids:
            notes.append(f"{sid} DROP: 현재 Event가 후보에 없음")
            continue
        if current_event_id in set(existing_by_id[sid]["event_ids"]):
            continue

        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            notes.append(f"{sid} DROP: reason 비어 있음")
            continue

        # 17_77 guard:
        # Candidate 생성기가 existing_membership_candidates에 넣어놓고도
        # reason에서 스스로 "직접 다루지 않는다 / 관련 없다"고 명시하는
        # 자기모순 출력을 Membership Validator까지 보내지 않는다.
        # 모델 의미판단을 새로 하는 규칙이 아니라, 같은 응답 안의 명시적
        # 자기부정을 deterministic하게 제거하는 안전장치다.
        reason_norm = re.sub(r"\s+", " ", reason.strip()).lower()
        self_reject_markers = (
            "직접 다루고 있지 않",
            "직접 다루지 않",
            "직접적으로 다루고 있지 않",
            "직접적으로 다루지 않",
            "관련이 없다",
            "관련이 없",
            "관련 없다",
            "관련없",
            "무관",
            "해당하지 않",
            "일치하지 않",
            "포함되지 않",
            "does not directly",
            "not directly",
            "unrelated",
        )
        if any(marker in reason_norm for marker in self_reject_markers):
            notes.append(
                f"{sid} DROP: Candidate reason이 기존 Subtopic과의 직접 관련을 스스로 부정"
            )
            continue

        normalized_existing.append({
            "subtopic_id": sid,
            "candidate_event_ids": [current_event_id],
            "reason": reason.strip(),
        })

    existing_name_norms = {
        normalize_text(item["name"])
        for item in state.get(
            "subtopics",
            [],
        )
    }
    existing_basis_norms = {
        normalize_text(item["common_basis"])
        for item in state.get(
            "subtopics",
            [],
        )
    }
    existing_criterion_norms = {
        normalize_text(item[
            "membership_criterion"
        ])
        for item in state.get(
            "subtopics",
            [],
        )
    }

    new_basis_norms = set()
    new_criterion_norms = set()
    normalized_new = []

    for i, item in enumerate(
        new_candidates,
        start=1,
    ):
        if not isinstance(item, dict):
            notes.append(
                f"new[{i}] DROP: 객체가 아님"
            )
            continue

        # -------------------------------------------------
        # CORE FIRST:
        # common_basis / criterion / candidate Event가
        # Subtopic 존재 여부를 결정한다.
        # name은 display-only라 이 단계의 DROP 사유가 아니다.
        # -------------------------------------------------
        common_basis = item.get(
            "common_basis"
        )

        if (
            not isinstance(common_basis, str)
            or not common_basis.strip()
        ):
            notes.append(
                f"new[{i}] DROP: common_basis 비어 있음"
            )
            continue

        common_basis = re.sub(
            r"\s+",
            " ",
            common_basis.strip(),
        )
        basis_norm = normalize_text(
            common_basis
        )

        stype = item.get("type")

        if stype not in ALLOWED_SUBTOPIC_TYPES:
            notes.append(
                f"new[{i}] DROP: 허용되지 않는 type={stype}"
            )
            continue

        if basis_norm in existing_basis_norms:
            notes.append(
                f"new[{i}] DROP: 기존 Subtopic과 같은 common_basis"
            )
            continue

        if basis_norm in new_basis_norms:
            notes.append(
                f"new[{i}] DROP: 같은 호출 내 중복 common_basis"
            )
            continue

        criterion = item.get(
            "membership_criterion"
        )

        if (
            not isinstance(criterion, str)
            or not criterion.strip()
        ):
            notes.append(
                f"new[{i}] DROP: membership_criterion 비어 있음"
            )
            continue

        try:
            criterion = (
                validate_event_local_membership_criterion(
                    criterion,
                    label=common_basis,
                )
            )
        except Exception as exc:
            notes.append(
                f"new[{i}] DROP: membership_criterion 오류={exc}"
            )
            continue

        criterion_norm = normalize_text(
            criterion
        )

        if (
            criterion_norm
            in existing_criterion_norms
        ):
            notes.append(
                f"new[{i}] DROP: 기존 Subtopic과 같은 membership_criterion"
            )
            continue

        if (
            criterion_norm
            in new_criterion_norms
        ):
            notes.append(
                f"new[{i}] DROP: 같은 호출 내 중복 membership_criterion"
            )
            continue

        ids = item.get(
            "candidate_event_ids"
        )

        if not isinstance(ids, list):
            notes.append(
                f"new[{i}] DROP: candidate_event_ids가 배열이 아님"
            )
            continue

        ids = list(
            dict.fromkeys(ids)
        )

        valid_ids = [
            eid
            for eid in ids
            if eid in available_event_ids
        ]

        invalid = [
            eid
            for eid in ids
            if eid not in available_event_ids
        ]

        if invalid:
            notes.append(
                f"new[{i}]: 존재하지 않는 Event 제거="
                + ",".join(
                    map(str, invalid)
                )
            )

        if (
            current_event_id
            not in valid_ids
        ):
            notes.append(
                f"new[{i}] DROP: 신규 후보에 현재 Event가 없음"
            )
            continue

        valid_ids = [
            current_event_id,
            *[
                eid
                for eid in valid_ids
                if eid
                != current_event_id
            ],
        ]

        if len(valid_ids) < 2:
            notes.append(
                f"new[{i}] DROP: 유효 candidate Event가 2개 미만"
            )
            continue

        reason = item.get("reason")

        if (
            not isinstance(reason, str)
            or not reason.strip()
        ):
            notes.append(
                f"new[{i}] DROP: reason 비어 있음"
            )
            continue

        # -------------------------------------------------
        # DISPLAY NAME LAST:
        # 이름이 나빠도 candidate를 버리지 않는다.
        # exact phrase-in-basis 요구도 없다.
        # -------------------------------------------------
        raw_name = item.get("name")

        name, name_status, name_warnings = (
            repair_display_name(
                raw_name,
                common_basis=common_basis,
            )
        )

        for warning in name_warnings:
            notes.append(
                f"new[{i}] display-name: {warning}"
            )

        name_norm = normalize_text(
            name
        )

        if name_norm in existing_name_norms:
            notes.append(
                f"{name}: 기존 Subtopic과 display name 중복 "
                "(후보 유지; 이름은 존재 판단에 사용하지 않음)"
            )

        if any(
            normalize_text(
                candidate["name"]
            )
            == name_norm
            for candidate
            in normalized_new
        ):
            notes.append(
                f"{name}: 같은 호출 내 display name 중복 "
                "(후보 유지; common_basis로 구분)"
            )

        new_basis_norms.add(
            basis_norm
        )
        new_criterion_norms.add(
            criterion_norm
        )

        normalized_new.append({
            "name": name,
            "display_name_status": (
                name_status
            ),
            "type": stype,
            "common_basis": common_basis,
            "membership_criterion": (
                criterion
            ),
            "candidate_event_ids": (
                valid_ids
            ),
            "reason": reason.strip(),
        })

    return {
        "current_event_id": current_event_id,
        "existing_membership_candidates": normalized_existing,
        "new_subtopic_candidates": normalized_new,
        "sanitization_notes": notes,
    }


# ---------------------------------------------------------
# Membership validation
# ---------------------------------------------------------

def validate_membership_output(
    raw: dict,
    *,
    event_profile: dict,
) -> dict:

    event_id = event_profile["event_id"]

    if raw.get("event_id") != event_id:
        raise ValueError(
            "validator.event_id가 입력 Event와 다릅니다."
        )

    relation = raw.get("relation")

    if relation not in {"DIRECT", "RELATED", "INDIRECT", "NONE"}:
        raise ValueError(
            "validator.relation은 DIRECT / RELATED / INDIRECT / NONE 중 하나여야 합니다."
        )

    related_path = raw.get("related_path")

    allowed_related_paths = {
        "SAME_CONCRETE_MECHANISM",
        "EXPLICIT_DIRECT_RELATION",
    }

    if relation == "RELATED":
        if related_path not in allowed_related_paths:
            raise ValueError(
                "relation=RELATED이면 related_path는 "
                "SAME_CONCRETE_MECHANISM / EXPLICIT_DIRECT_RELATION 중 하나여야 합니다."
            )
    elif related_path is not None:
        raise ValueError(
            f"relation={relation}이면 related_path는 null이어야 합니다."
        )

    evidence_scope = raw.get("evidence_scope")

    expected_scope_by_relation = {
        "DIRECT": "PRIMARY",
        "RELATED": "DIRECT_CONTEXT",
        "INDIRECT": "BACKGROUND_ONLY",
        "NONE": None,
    }

    expected_scope = expected_scope_by_relation[relation]

    if evidence_scope != expected_scope:
        raise ValueError(
            f"relation={relation}이면 evidence_scope는 "
            f"{expected_scope!r}여야 합니다."
        )

    source = raw.get("source")
    anchor_name = raw.get("anchor_name")
    sentence_index = raw.get("summary_sentence_index")

    reason = raw.get("reason")

    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("validator.reason이 비어 있습니다.")

    anchors = [
        item.get("name")
        for item in event_profile.get("anchors", [])
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and item.get("name").strip()
    ]

    anchor_map = {
        normalize_text(name): name
        for name in anchors
    }

    summary_sentences = split_summary_sentences(
        event_profile.get("event_summary") or ""
    )

    if relation == "NONE":
        if source is not None:
            raise ValueError(
                "relation=NONE이면 source는 null이어야 합니다."
            )
        if anchor_name is not None:
            raise ValueError(
                "relation=NONE이면 anchor_name은 null이어야 합니다."
            )
        if sentence_index is not None:
            raise ValueError(
                "relation=NONE이면 summary_sentence_index는 null이어야 합니다."
            )

        return {
            "event_id": event_id,
            "relation": "NONE",
            "related_path": None,
            "evidence_scope": None,
            "decision": "REJECT",
            "source": None,
            "anchor_name": None,
            "summary_sentence_index": None,
            "evidence_text": None,
            "reason": reason.strip(),
        }

    # periodic7:
    # DIRECT는 EVENT_NAME만 source로 허용한다.
    # summary/anchor로 DIRECT를 만들 수 없도록 Python에서 강제한다.
    if relation == "DIRECT":
        if source != "EVENT_NAME":
            raise ValueError(
                "relation=DIRECT이면 source는 반드시 EVENT_NAME이어야 합니다."
            )

        if anchor_name is not None:
            raise ValueError(
                "DIRECT + EVENT_NAME이면 anchor_name은 null이어야 합니다."
            )

        if sentence_index is not None:
            raise ValueError(
                "DIRECT + EVENT_NAME이면 summary_sentence_index는 null이어야 합니다."
            )

        normalized_anchor = None
        normalized_sentence_index = None
        evidence_text = event_profile["event_name"]

    else:
        if source not in {"ANCHOR", "EVENT_SUMMARY"}:
            raise ValueError(
                f"relation={relation}이면 source는 ANCHOR 또는 EVENT_SUMMARY여야 합니다."
            )

        if (
            relation == "RELATED"
            and related_path == "EXPLICIT_DIRECT_RELATION"
            and source != "EVENT_SUMMARY"
        ):
            raise ValueError(
                "RELATED + EXPLICIT_DIRECT_RELATION이면 "
                "source는 반드시 EVENT_SUMMARY여야 합니다."
            )

        if source == "ANCHOR":
            if not isinstance(anchor_name, str) or not anchor_name.strip():
                raise ValueError(
                    "ANCHOR 근거인데 anchor_name이 없습니다."
                )

            if sentence_index is not None:
                raise ValueError(
                    "ANCHOR 근거이면 summary_sentence_index는 null이어야 합니다."
                )

            anchor_key = normalize_text(anchor_name)

            if anchor_key not in anchor_map:
                raise ValueError(
                    "실제 Event Profile anchors에 없는 anchor입니다: "
                    f"{anchor_name}"
                )

            normalized_anchor = anchor_map[anchor_key]
            normalized_sentence_index = None
            evidence_text = normalized_anchor

        else:
            if anchor_name is not None:
                raise ValueError(
                    "EVENT_SUMMARY 근거이면 anchor_name은 null이어야 합니다."
                )

            if not isinstance(sentence_index, int):
                raise ValueError(
                    "EVENT_SUMMARY 근거인데 summary_sentence_index가 정수가 아닙니다."
                )

            if not (0 <= sentence_index < len(summary_sentences)):
                raise ValueError(
                    f"summary_sentence_index가 범위를 벗어났습니다: "
                    f"{sentence_index} (문장 수={len(summary_sentences)})"
                )

            normalized_anchor = None
            normalized_sentence_index = sentence_index
            evidence_text = summary_sentences[sentence_index]

    # periodic7: 실제 Subtopic Membership은 DIRECT만 허용하며 DIRECT 근거는 EVENT_NAME이다.
    # RELATED는 진단값으로 기록하되 실제 귀속에는 사용하지 않는다.
    decision = (
        "KEEP"
        if relation == "DIRECT"
        else "REJECT"
    )

    return {
        "event_id": event_id,
        "relation": relation,
        "related_path": related_path,
        "evidence_scope": evidence_scope,
        "decision": decision,
        "source": source,
        "anchor_name": normalized_anchor,
        "summary_sentence_index": normalized_sentence_index,
        "evidence_text": evidence_text,
        "reason": reason.strip(),
    }




# ---------------------------------------------------------
# Subtopic display refinement
# ---------------------------------------------------------

def validate_refinement_output(
    raw: dict,
    *,
    subtopic: dict,
) -> dict:

    if raw.get("subtopic_id") != subtopic["subtopic_id"]:
        raise ValueError(
            "refiner.subtopic_id가 입력 Subtopic과 다릅니다."
        )

    action = raw.get("action")

    if action not in {"KEEP", "UPDATE"}:
        raise ValueError(
            "refiner.action은 KEEP / UPDATE 중 하나여야 합니다."
        )

    new_name = raw.get("new_name")
    new_common_basis = raw.get("new_common_basis")
    new_membership_criterion = raw.get("new_membership_criterion")
    reason = raw.get("reason")

    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("refiner.reason이 비어 있습니다.")

    if action == "KEEP":
        if (
            new_name is not None
            or new_common_basis is not None
            or new_membership_criterion is not None
        ):
            raise ValueError(
                "action=KEEP이면 new_name/new_common_basis/"
                "new_membership_criterion은 null이어야 합니다."
            )

        return {
            "subtopic_id": subtopic["subtopic_id"],
            "action": "KEEP",
            "new_name": None,
            "new_common_basis": None,
            "new_membership_criterion": None,
            "reason": reason.strip(),
        }

    if not isinstance(new_name, str) or not new_name.strip():
        raise ValueError("action=UPDATE인데 new_name이 비어 있습니다.")

    if not isinstance(new_common_basis, str) or not new_common_basis.strip():
        raise ValueError(
            "action=UPDATE인데 new_common_basis가 비어 있습니다."
        )

    if (
        not isinstance(new_membership_criterion, str)
        or not new_membership_criterion.strip()
    ):
        raise ValueError(
            "action=UPDATE인데 new_membership_criterion이 비어 있습니다."
        )

    new_name = re.sub(r"\s+", " ", new_name.strip())
    new_common_basis = re.sub(r"\s+", " ", new_common_basis.strip())
    new_membership_criterion = validate_event_local_membership_criterion(
        new_membership_criterion,
        label=new_name,
    )

    if (
        normalize_text(new_name) == normalize_text(subtopic["name"])
        and normalize_text(new_common_basis)
        == normalize_text(subtopic["common_basis"])
        and normalize_text(new_membership_criterion)
        == normalize_text(subtopic["membership_criterion"])
    ):
        raise ValueError(
            "action=UPDATE인데 name/common_basis/"
            "membership_criterion이 기존과 모두 동일합니다."
        )

    return {
        "subtopic_id": subtopic["subtopic_id"],
        "action": "UPDATE",
        "new_name": new_name,
        "new_common_basis": new_common_basis,
        "new_membership_criterion": new_membership_criterion,
        "reason": reason.strip(),
    }


def refine_subtopic_display(
    *,
    client: OpenAI,
    model: str,
    refiner_prompt: str,
    topic_name: str,
    subtopic: dict,
    member_events: list[dict],
    member_membership_evidence: list[dict],
    temperature: float,
    max_tokens: int,
    json_mode: bool,
    max_retries: int,
    retry_wait: float,
) -> dict:

    payload = build_refiner_payload(
        topic_name=topic_name,
        subtopic=subtopic,
        member_events=member_events,
        member_membership_evidence=member_membership_evidence,
    )

    return run_json_call_with_retries(
        client=client,
        model=model,
        prompt=refiner_prompt,
        payload=payload,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=json_mode,
        max_retries=max_retries,
        retry_wait=retry_wait,
        validate_fn=lambda raw: validate_refinement_output(
            raw,
            subtopic=subtopic,
        ),
    )


def run_json_call_with_retries(
    *,
    client: OpenAI,
    model: str,
    prompt: str,
    payload: str,
    temperature: float,
    max_tokens: int,
    json_mode: bool,
    max_retries: int,
    retry_wait: float,
    validate_fn,
):

    last_error = None
    validation_feedback = None
    previous_bad_output = None

    for attempt in range(1, max_retries + 1):
        started = time.time()
        content = None

        try:
            response = call_model(
                client=client,
                model=model,
                prompt=prompt,
                payload=payload,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=json_mode,
                validation_feedback=validation_feedback,
                previous_bad_output=previous_bad_output,
            )

            content = extract_text_content(
                response.choices[0].message.content
            )
            raw = parse_json_response(content)
            normalized = validate_fn(raw)

            return {
                "result": normalized,
                "attempt": attempt,
                "elapsed_sec": round(time.time() - started, 3),
                "usage": usage_dict(response),
            }

        except Exception as e:
            last_error = e
            validation_feedback = str(e)

            if content:
                previous_bad_output = content

            if attempt < max_retries:
                wait = retry_wait * attempt

                print(
                    f"    [재시도 {attempt}/{max_retries}] "
                    f"{type(e).__name__}: {e} -> {wait:.1f}초",
                    file=sys.stderr,
                )
                time.sleep(wait)

    raise last_error


# ---------------------------------------------------------
# Candidate -> independent membership validation
# ---------------------------------------------------------

def validate_one_membership(
    *,
    client: OpenAI,
    model: str,
    validator_prompt: str,
    topic_name: str,
    subtopic: dict,
    event_profile: dict,
    temperature: float,
    max_tokens: int,
    json_mode: bool,
    max_retries: int,
    retry_wait: float,
) -> dict:

    payload = build_validator_payload(
        topic_name=topic_name,
        subtopic=subtopic,
        event_profile=event_profile,
    )

    call = run_json_call_with_retries(
        client=client,
        model=model,
        prompt=validator_prompt,
        payload=payload,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=json_mode,
        max_retries=max_retries,
        retry_wait=retry_wait,
        validate_fn=lambda raw: validate_membership_output(
            raw,
            event_profile=event_profile,
        ),
    )

    return call



def run_membership_tasks_parallel(
    *,
    tasks: list[dict],
    client: OpenAI,
    model: str,
    validator_prompt: str,
    topic_name: str,
    event_by_id: dict[str, dict],
    temperature: float,
    max_tokens: int,
    json_mode: bool,
    max_retries: int,
    retry_wait: float,
    workers: int,
) -> list[dict]:
    """
    각 Membership 판정은 원래 v1.8.8처럼
    Subtopic 1개 + Event 1개만 보는 독립 요청이다.

    fast1은 이 독립 요청들을 동시에 실행해 wall-clock time만 줄인다.
    한 요청 안에 여러 Event를 합치지 않으므로 판정 의미는 유지한다.
    """

    if not tasks:
        return []

    def one(task: dict) -> dict:
        call = validate_one_membership(
            client=client,
            model=model,
            validator_prompt=validator_prompt,
            topic_name=topic_name,
            subtopic=task["subtopic"],
            event_profile=event_by_id[task["event_id"]],
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
            max_retries=max_retries,
            retry_wait=retry_wait,
        )

        return {
            "task": task,
            "call": call,
        }

    if workers <= 1 or len(tasks) == 1:
        return [one(task) for task in tasks]

    ordered = [None] * len(tasks)

    with ThreadPoolExecutor(
        max_workers=min(workers, len(tasks))
    ) as executor:
        future_to_index = {
            executor.submit(one, task): index
            for index, task in enumerate(tasks)
        }

        for future in as_completed(future_to_index):
            index = future_to_index[future]
            ordered[index] = future.result()

    return ordered


def append_membership_result_log(
    *,
    membership_log_path: Path,
    step_number: int,
    current_event_id: str,
    result_item: dict,
    model: str,
) -> None:

    task = result_item["task"]
    call = result_item["call"]
    validation = call["result"]

    row = {
        "step": step_number,
        "trigger_event_id": current_event_id,
        "target_kind": task["target_kind"],
        "candidate_event_id": task["event_id"],
        "validation": validation,
        "model": model,
        "attempt": call["attempt"],
        "elapsed_sec": call["elapsed_sec"],
        "usage": call["usage"],
        "phase": task["phase"],
    }

    if task["target_kind"] == "EXISTING_SUBTOPIC":
        row.update({
            "target_subtopic_id": task["subtopic_id"],
            "target_name": task["subtopic"]["name"],
            "common_basis": task["subtopic"]["common_basis"],
            "membership_criterion": task["subtopic"][
                "membership_criterion"
            ],
        })
    else:
        row.update({
            "candidate_index": task["candidate_index"],
            "target_name": task["subtopic"]["name"],
            "common_basis": task["subtopic"]["common_basis"],
            "membership_criterion": task["subtopic"][
                "membership_criterion"
            ],
        })

    append_jsonl(membership_log_path, row)


def build_final_decision(
    *,
    client: OpenAI,
    model: str,
    validator_prompt: str,
    topic_name: str,
    candidate_decision: dict,
    state: dict,
    event_by_id: dict[str, dict],
    validator_temperature: float,
    validator_max_tokens: int,
    json_mode: bool,
    max_retries: int,
    retry_wait: float,
    validator_workers: int,
    min_new_keep: int,
    step_number: int,
    current_event_id: str,
    membership_log_path: Path,
) -> dict:

    existing_by_id = {
        item["subtopic_id"]: item
        for item in state.get("subtopics", [])
    }

    final_existing_updates = []
    final_new_subtopics = []
    dropped_new_candidates = []

    validator_call_count = 0
    refiner_call_count = 0

    # -----------------------------------------------------
    # Phase A
    #
    # 1) 기존 Subtopic: 현재 Event만 검증
    # 2) 신규 후보: 현재 Event를 먼저 검증
    #
    # 신규 후보는 현재 Event가 DIRECT/PRIMARY일 때만
    # 과거 후보 Event 검증으로 넘어간다.
    # -----------------------------------------------------
    phase_a_tasks = []

    for candidate in candidate_decision[
        "existing_membership_candidates"
    ]:
        sid = candidate["subtopic_id"]
        subtopic = existing_by_id[sid]

        # sanitizer가 current only로 만들지만 안전하게 다시 고정.
        if current_event_id not in candidate["candidate_event_ids"]:
            continue

        phase_a_tasks.append({
            "phase": "A_EXISTING_CURRENT",
            "target_kind": "EXISTING_SUBTOPIC",
            "subtopic_id": sid,
            "subtopic": subtopic,
            "event_id": current_event_id,
            "candidate_reason": candidate["reason"],
        })

    new_candidates_by_index = {}

    for candidate_index, candidate in enumerate(
        candidate_decision["new_subtopic_candidates"],
        start=1,
    ):
        if current_event_id not in candidate["candidate_event_ids"]:
            dropped_new_candidates.append({
                "name": candidate["name"],
                "type": candidate["type"],
                "membership_criterion": candidate["membership_criterion"],
                "common_basis": candidate["common_basis"],
                "candidate_event_ids": candidate["candidate_event_ids"],
                "kept_event_ids": [],
                "drop_reason": (
                    "신규 후보에 현재 Event가 없음"
                ),
                "all_validations": [],
            })
            continue

        candidate_subtopic = {
            "name": candidate["name"],
            "display_name_status": candidate.get(
                "display_name_status",
                "OK",
            ),
            "type": candidate["type"],
            "common_basis": candidate["common_basis"],
            "membership_criterion": candidate["membership_criterion"],
        }

        new_candidates_by_index[candidate_index] = {
            "candidate": candidate,
            "subtopic": candidate_subtopic,
            "validations": [],
            "kept_ids": [],
            "trigger_direct": False,
        }

        phase_a_tasks.append({
            "phase": "A_NEW_TRIGGER",
            "target_kind": "NEW_SUBTOPIC_CANDIDATE",
            "candidate_index": candidate_index,
            "subtopic": candidate_subtopic,
            "event_id": current_event_id,
        })

    phase_a_results = run_membership_tasks_parallel(
        tasks=phase_a_tasks,
        client=client,
        model=model,
        validator_prompt=validator_prompt,
        topic_name=topic_name,
        event_by_id=event_by_id,
        temperature=validator_temperature,
        max_tokens=validator_max_tokens,
        json_mode=json_mode,
        max_retries=max_retries,
        retry_wait=retry_wait,
        workers=validator_workers,
    )

    validator_call_count += len(phase_a_results)

    for result_item in phase_a_results:
        append_membership_result_log(
            membership_log_path=membership_log_path,
            step_number=step_number,
            current_event_id=current_event_id,
            result_item=result_item,
            model=model,
        )

        task = result_item["task"]
        validation = result_item["call"]["result"]

        if task["target_kind"] == "EXISTING_SUBTOPIC":
            if validation["decision"] == "KEEP":
                final_existing_updates.append({
                    "subtopic_id": task["subtopic_id"],
                    "add_event_ids": [current_event_id],
                    "membership_validations": [validation],
                    "candidate_reason": task["candidate_reason"],
                })
            continue

        candidate_index = task["candidate_index"]
        holder = new_candidates_by_index[candidate_index]
        holder["validations"].append(validation)

        if validation["decision"] == "KEEP":
            holder["kept_ids"].append(current_event_id)

        # 핵심 비용/품질 gate:
        # 신규 Subtopic은 현재 Event가 그 focal point 자체를 다루는
        # DIRECT / PRIMARY일 때만 계속 검증한다.
        if (
            validation["relation"] == "DIRECT"
            and validation["evidence_scope"] == "PRIMARY"
        ):
            holder["trigger_direct"] = True

    # -----------------------------------------------------
    # Phase B
    # 살아남은 신규 후보의 과거 Event만 병렬 검증.
    # trigger가 RELATED/INDIRECT/NONE이면 여기서 비용을 쓰지 않는다.
    # -----------------------------------------------------
    phase_b_tasks = []

    for candidate_index, holder in new_candidates_by_index.items():
        candidate = holder["candidate"]

        if not holder["trigger_direct"]:
            dropped_new_candidates.append({
                "name": candidate["name"],
                "type": candidate["type"],
                "membership_criterion": candidate["membership_criterion"],
                "common_basis": candidate["common_basis"],
                "candidate_event_ids": candidate["candidate_event_ids"],
                "kept_event_ids": list(holder["kept_ids"]),
                "drop_reason": (
                    "현재 Event가 신규 Subtopic의 DIRECT/PRIMARY가 아님"
                ),
                "all_validations": list(holder["validations"]),
            })
            continue

        for event_id in candidate["candidate_event_ids"]:
            if event_id == current_event_id:
                continue

            phase_b_tasks.append({
                "phase": "B_NEW_PAST",
                "target_kind": "NEW_SUBTOPIC_CANDIDATE",
                "candidate_index": candidate_index,
                "subtopic": holder["subtopic"],
                "event_id": event_id,
            })

    phase_b_results = run_membership_tasks_parallel(
        tasks=phase_b_tasks,
        client=client,
        model=model,
        validator_prompt=validator_prompt,
        topic_name=topic_name,
        event_by_id=event_by_id,
        temperature=validator_temperature,
        max_tokens=validator_max_tokens,
        json_mode=json_mode,
        max_retries=max_retries,
        retry_wait=retry_wait,
        workers=validator_workers,
    )

    validator_call_count += len(phase_b_results)

    for result_item in phase_b_results:
        append_membership_result_log(
            membership_log_path=membership_log_path,
            step_number=step_number,
            current_event_id=current_event_id,
            result_item=result_item,
            model=model,
        )

        task = result_item["task"]
        validation = result_item["call"]["result"]
        holder = new_candidates_by_index[task["candidate_index"]]

        holder["validations"].append(validation)

        if validation["decision"] == "KEEP":
            holder["kept_ids"].append(task["event_id"])

    # -----------------------------------------------------
    # Finalize new subtopics
    # -----------------------------------------------------
    already_dropped_indices = {
        index
        for index, holder in new_candidates_by_index.items()
        if not holder["trigger_direct"]
    }

    for candidate_index, holder in new_candidates_by_index.items():
        if candidate_index in already_dropped_indices:
            continue

        candidate = holder["candidate"]
        kept_ids = list(dict.fromkeys(holder["kept_ids"]))

        if len(kept_ids) < min_new_keep:
            dropped_new_candidates.append({
                "name": candidate["name"],
                "type": candidate["type"],
                "membership_criterion": candidate["membership_criterion"],
                "common_basis": candidate["common_basis"],
                "candidate_event_ids": candidate["candidate_event_ids"],
                "kept_event_ids": kept_ids,
                "drop_reason": (
                    f"Membership Validator KEEP Event가 "
                    f"{min_new_keep}개 미만"
                ),
                "all_validations": list(holder["validations"]),
            })
            continue

        kept_validations = [
            item
            for item in holder["validations"]
            if item["decision"] == "KEEP"
        ]

        final_new_subtopics.append({
            "name": candidate["name"],
            "display_name_status": candidate.get(
                "display_name_status",
                "OK",
            ),
            "type": candidate["type"],
            "membership_criterion": candidate["membership_criterion"],
            "common_basis": candidate["common_basis"],
            "event_ids": kept_ids,
            "membership_validations": kept_validations,
            "candidate_reason": candidate["reason"],
            # fast1은 streaming 중 Refiner를 실행하지 않는다.
            "creation_scope_refinement": None,
        })

    # 겹침은 진단만 기록.
    overlap_report = []

    for i in range(len(final_new_subtopics)):
        for j in range(i + 1, len(final_new_subtopics)):
            a = final_new_subtopics[i]
            b = final_new_subtopics[j]

            shared = sorted(
                set(a["event_ids"]) & set(b["event_ids"])
            )

            if shared:
                overlap_report.append({
                    "name_a": a["name"],
                    "name_b": b["name"],
                    "shared_event_ids": shared,
                    "shared_count": len(shared),
                    "a_event_count": len(a["event_ids"]),
                    "b_event_count": len(b["event_ids"]),
                })

    return {
        "current_event_id": current_event_id,
        "existing_subtopic_updates": final_existing_updates,
        "new_subtopics": final_new_subtopics,
        "dropped_new_candidates": dropped_new_candidates,
        "new_subtopic_overlap_report": overlap_report,
        "validator_call_count": validator_call_count,
        "refiner_call_count": refiner_call_count,
    }


def apply_final_decision(
    *,
    state: dict,
    final_decision: dict,
    current_event: dict,
    step_number: int,
) -> dict:

    by_id = {
        item["subtopic_id"]: item
        for item in state["subtopics"]
    }

    for update in final_decision["existing_subtopic_updates"]:
        subtopic = by_id[update["subtopic_id"]]
        before = set(subtopic["event_ids"])

        added_ids = []

        for event_id in update["add_event_ids"]:
            if event_id not in before:
                subtopic["event_ids"].append(event_id)
                added_ids.append(event_id)

        if added_ids:
            kept_validation_by_id = {
                item["event_id"]: item
                for item in update["membership_validations"]
            }

            subtopic.setdefault("update_history", []).append({
                "step": step_number,
                "trigger_event_id": current_event["event_id"],
                "added_event_ids": added_ids,
                "membership_validations": [
                    kept_validation_by_id[event_id]
                    for event_id in added_ids
                ],
                "candidate_reason": update["candidate_reason"],
            })

            refinement = update.get("display_refinement")

            if refinement and refinement["action"] == "UPDATE":
                before_name = subtopic["name"]
                before_common_basis = subtopic["common_basis"]
                before_membership_criterion = subtopic[
                    "membership_criterion"
                ]

                subtopic["name"] = refinement["new_name"]
                subtopic["common_basis"] = refinement["new_common_basis"]
                subtopic["membership_criterion"] = refinement[
                    "new_membership_criterion"
                ]

                subtopic.setdefault("refinement_history", []).append({
                    "step": step_number,
                    "trigger_event_id": current_event["event_id"],
                    "before_name": before_name,
                    "after_name": subtopic["name"],
                    "before_common_basis": before_common_basis,
                    "after_common_basis": subtopic["common_basis"],
                    "before_membership_criterion": (
                        before_membership_criterion
                    ),
                    "after_membership_criterion": subtopic[
                        "membership_criterion"
                    ],
                    "applies_to_future_membership_from_step": (
                        step_number + 1
                    ),
                    "past_memberships_revalidated": False,
                    "reason": refinement["reason"],
                })

    for item in final_decision["new_subtopics"]:
        sid = next_subtopic_id(state)

        assigned_sid = item.get("assigned_subtopic_id")

        if assigned_sid is not None and assigned_sid != sid:
            raise ValueError(
                "신규 Subtopic에 미리 할당한 ID와 실제 state ID가 다릅니다: "
                f"{assigned_sid} != {sid}"
            )

        refinement = item.get("creation_scope_refinement")

        final_name = item["name"]
        final_common_basis = item["common_basis"]
        final_membership_criterion = item["membership_criterion"]

        creation_refinement_history = []

        if refinement and refinement["action"] == "UPDATE":
            final_name = refinement["new_name"]
            final_common_basis = refinement["new_common_basis"]
            final_membership_criterion = refinement[
                "new_membership_criterion"
            ]

            creation_refinement_history.append({
                "step": step_number,
                "trigger_event_id": current_event["event_id"],
                "kind": "CREATION_REFINEMENT",
                "before_name": item["name"],
                "after_name": final_name,
                "before_common_basis": item["common_basis"],
                "after_common_basis": final_common_basis,
                "before_membership_criterion": item[
                    "membership_criterion"
                ],
                "after_membership_criterion": (
                    final_membership_criterion
                ),
                "applies_to_future_membership_from_step": (
                    step_number + 1
                ),
                "past_memberships_revalidated": False,
                "reason": refinement["reason"],
            })

        new_subtopic = {
            "subtopic_id": sid,
            "name": final_name,
            "display_name_status": item.get(
                "display_name_status",
                "OK",
            ),
            "type": item["type"],
            "common_basis": final_common_basis,
            "membership_criterion": final_membership_criterion,
            "event_ids": list(item["event_ids"]),
            "creation_membership_validations": list(
                item["membership_validations"]
            ),
            "created_at_step": step_number,
            "created_by_event_id": current_event["event_id"],
            "candidate_reason": item["candidate_reason"],
            "creation_scope_refinement": refinement,
            "refinement_history": creation_refinement_history,
            "update_history": [],
        }

        state["subtopics"].append(new_subtopic)
        state["next_subtopic_number"] += 1

    if current_event["event_id"] not in state["processed_event_ids"]:
        state["processed_event_ids"].append(current_event["event_id"])

    state["last_processed_event_id"] = current_event["event_id"]
    state["last_processed_step"] = step_number

    return state



# ---------------------------------------------------------
# Periodic canonical Subtopic consolidation
# ---------------------------------------------------------

CONSOLIDATION_VERSION = "v1.8.8-periodic7"


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value.strip())
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return str(value)


def _extract_known_ids(value: Any, *, allowed_ids: set[str], object_keys: tuple[str, ...]) -> list[str]:
    found = []
    def walk(item: Any) -> None:
        if item is None:
            return
        if isinstance(item, str):
            raw = item.strip()
            if not raw:
                return
            if raw in allowed_ids:
                found.append(raw); return
            if raw.startswith("[") and raw.endswith("]"):
                try:
                    walk(json.loads(raw)); return
                except Exception:
                    pass
            matches=[]
            for known in allowed_ids:
                m=re.search(r"(?<![A-Za-z0-9_-])"+re.escape(known)+r"(?![A-Za-z0-9_-])",raw)
                if m: matches.append((m.start(),known))
            for _,known in sorted(matches): found.append(known)
            return
        if isinstance(item, dict):
            for key in object_keys:
                if key in item:
                    walk(item[key]); return
            for key in item:
                if isinstance(key,str) and key in allowed_ids: found.append(key)
            return
        if isinstance(item,(list,tuple,set)):
            for child in item: walk(child)
    walk(value)
    result=[]; seen=set()
    for item in found:
        if item in allowed_ids and item not in seen:
            seen.add(item); result.append(item)
    return result


def _sum_usage(*usage_dicts: dict) -> dict:
    keys=set()
    for usage in usage_dicts:
        keys.update(k for k,v in usage.items() if isinstance(v,(int,float)))
    return {k:sum(u.get(k,0) for u in usage_dicts if isinstance(u.get(k,0),(int,float))) for k in keys}


def build_subtopic_overlap_pairs(subtopics: list[dict]) -> list[dict]:
    pairs=[]
    for i in range(len(subtopics)):
        a=subtopics[i]; a_ids=set(a.get("event_ids",[]))
        for j in range(i+1,len(subtopics)):
            b=subtopics[j]; b_ids=set(b.get("event_ids",[]))
            shared=sorted(a_ids & b_ids)
            if not shared: continue
            union=a_ids|b_ids
            pairs.append({
                "subtopic_id_a":a["subtopic_id"],"name_a":a["name"],
                "subtopic_id_b":b["subtopic_id"],"name_b":b["name"],
                "shared_event_ids":shared,"shared_count":len(shared),
                "jaccard":round(len(shared)/len(union),4) if union else 0.0,
            })
    pairs.sort(key=lambda x:(-x["shared_count"],-x["jaccard"],x["subtopic_id_a"],x["subtopic_id_b"]))
    return pairs


def build_consolidation_planner_payload(*,topic_name:str,checkpoint_step:int,state:dict,event_by_id:dict[str,dict])->str:
    subtopics=state.get("subtopics",[]); event_ids=set(); active=[]
    for st in subtopics:
        ids=list(dict.fromkeys(st.get("event_ids",[]))); event_ids.update(ids)
        active.append({
            "subtopic_id":st["subtopic_id"],"name":st["name"],"type":st["type"],
            "common_basis":st["common_basis"],"membership_criterion":st["membership_criterion"],"event_ids":ids,
        })
    event_index=[{"event_id":eid,"event_name":event_by_id[eid]["event_name"]}
                 for eid in sorted(event_ids,key=lambda eid:event_sort_key(event_by_id[eid]))]
    return json.dumps({"topic_name":topic_name,"checkpoint_step":checkpoint_step,
                       "active_subtopics":active,"event_index":event_index,
                       "overlap_pairs":build_subtopic_overlap_pairs(subtopics)},ensure_ascii=False,indent=2)


def _pair_key(a:str,b:str)->tuple[str,str]: return tuple(sorted((a,b)))


def normalize_consolidation_plan(
    raw: Any,
    *,
    state: dict,
) -> dict:
    """
    Planner 출력은 아직 최종 merge가 아니다.

    DUPLICATE는 second-stage validator로 보내고,
    CONTAINED_BY는 이후 Python strict checks를 거친다.
    """
    warnings = []

    subtopics = state.get(
        "subtopics",
        [],
    )
    by_id = {
        item["subtopic_id"]: item
        for item in subtopics
    }
    known = set(by_id)

    if not isinstance(raw, dict):
        raw = {}
        warnings.append(
            "planner 응답 형식 오류 -> 관계 없음"
        )

    dup_raw = raw.get(
        "duplicate_pairs",
        [],
    )
    cont_raw = raw.get(
        "contained_by",
        [],
    )

    if isinstance(dup_raw, dict):
        dup_raw = [dup_raw]

    if not isinstance(dup_raw, list):
        dup_raw = []

    if isinstance(cont_raw, dict):
        cont_raw = [cont_raw]

    if not isinstance(cont_raw, list):
        cont_raw = []

    duplicate_candidates = []
    seen_pairs = set()

    for i, item in enumerate(
        dup_raw,
        start=1,
    ):
        if not isinstance(item, dict):
            continue

        if (
            item.get(
                "mutual_membership_equivalent"
            )
            is not True
        ):
            continue

        left = _extract_known_ids(
            item.get(
                "left_subtopic_id"
            ),
            allowed_ids=known,
            object_keys=(
                "left_subtopic_id",
                "subtopic_id",
                "id",
            ),
        )
        right = _extract_known_ids(
            item.get(
                "right_subtopic_id"
            ),
            allowed_ids=known,
            object_keys=(
                "right_subtopic_id",
                "subtopic_id",
                "id",
            ),
        )

        if (
            not left
            or not right
            or left[0] == right[0]
        ):
            continue

        pair = _pair_key(
            left[0],
            right[0],
        )

        if pair in seen_pairs:
            continue

        # 2차 validator 비용도 아끼기 위해
        # 관측 Event가 하나도 겹치지 않는 pair는 보내지 않는다.
        shared = (
            set(
                by_id[pair[0]].get(
                    "event_ids",
                    [],
                )
            )
            & set(
                by_id[pair[1]].get(
                    "event_ids",
                    [],
                )
            )
        )

        if not shared:
            warnings.append(
                f"duplicate pair[{i}] shared Event 없음 -> validator 전 무시"
            )
            continue

        seen_pairs.add(pair)

        duplicate_candidates.append({
            "pair_id": (
                f"P{len(duplicate_candidates)+1:03d}"
            ),
            "left_subtopic_id": pair[0],
            "right_subtopic_id": pair[1],
            "shared_event_ids": sorted(
                shared
            ),
        })

    contained_candidates = []

    for i, item in enumerate(
        cont_raw,
        start=1,
    ):
        if not isinstance(item, dict):
            continue

        if (
            item.get(
                "same_process_instance"
            )
            is not True
            or item.get(
                "child_scope_subset"
            )
            is not True
        ):
            continue

        child = _extract_known_ids(
            item.get(
                "child_subtopic_id"
            ),
            allowed_ids=known,
            object_keys=(
                "child_subtopic_id",
                "subtopic_id",
                "id",
            ),
        )
        container = _extract_known_ids(
            item.get(
                "container_subtopic_id"
            ),
            allowed_ids=known,
            object_keys=(
                "container_subtopic_id",
                "subtopic_id",
                "id",
            ),
        )

        if (
            not child
            or not container
            or child[0] == container[0]
        ):
            continue

        contained_candidates.append({
            "child_subtopic_id": (
                child[0]
            ),
            "container_subtopic_id": (
                container[0]
            ),
        })

    return {
        "duplicate_candidates": (
            duplicate_candidates
        ),
        "contained_candidates": (
            contained_candidates
        ),
        "warnings": warnings,
    }


def build_duplicate_validator_payload(
    *,
    topic_name: str,
    checkpoint_step: int,
    duplicate_candidates: list[dict],
    state: dict,
    event_by_id: dict[str, dict],
) -> str:
    by_id = {
        item["subtopic_id"]: item
        for item in state.get(
            "subtopics",
            [],
        )
    }

    def compact_subtopic(
        sid: str,
    ) -> dict:
        subtopic = by_id[sid]

        event_names = [
            event_by_id[event_id][
                "event_name"
            ]
            for event_id in subtopic.get(
                "event_ids",
                [],
            )
            if event_id in event_by_id
        ]

        return {
            "subtopic_id": sid,
            "name": subtopic.get(
                "name"
            ),
            "common_basis": subtopic.get(
                "common_basis"
            ),
            "membership_criterion": (
                subtopic.get(
                    "membership_criterion"
                )
            ),
            "member_event_names": (
                event_names
            ),
        }

    pairs = []

    for item in duplicate_candidates:
        pairs.append({
            "pair_id": item["pair_id"],
            "left": compact_subtopic(
                item[
                    "left_subtopic_id"
                ]
            ),
            "right": compact_subtopic(
                item[
                    "right_subtopic_id"
                ]
            ),
            "shared_event_ids": item[
                "shared_event_ids"
            ],
        })

    return json.dumps(
        {
            "topic_name": topic_name,
            "checkpoint_step": (
                checkpoint_step
            ),
            "pairs": pairs,
        },
        ensure_ascii=False,
        indent=2,
    )


def normalize_duplicate_validation_output(
    raw: Any,
    *,
    duplicate_candidates: list[dict],
) -> dict:
    expected = {
        item["pair_id"]: item
        for item in duplicate_candidates
    }

    if isinstance(raw, dict):
        decisions_raw = raw.get(
            "decisions",
            [],
        )
    elif isinstance(raw, list):
        decisions_raw = raw
    else:
        decisions_raw = []

    if isinstance(decisions_raw, dict):
        decisions_raw = [
            decisions_raw
        ]

    if not isinstance(
        decisions_raw,
        list,
    ):
        decisions_raw = []

    decisions = {}

    for item in decisions_raw:
        if not isinstance(item, dict):
            continue

        pair_id = _safe_text(
            item.get("pair_id")
        )

        if pair_id not in expected:
            continue

        left_accepts = (
            item.get(
                "left_accepts_right"
            )
            is True
        )
        right_accepts = (
            item.get(
                "right_accepts_left"
            )
            is True
        )
        raw_decision = _safe_text(
            item.get("decision")
        ).upper()

        decision = (
            "SAME_FILTER"
            if (
                raw_decision
                == "SAME_FILTER"
                and left_accepts
                and right_accepts
            )
            else "DISTINCT_FILTER"
        )

        decisions.setdefault(
            pair_id,
            {
                "left_accepts_right": (
                    left_accepts
                ),
                "right_accepts_left": (
                    right_accepts
                ),
                "decision": decision,
            },
        )

    # Missing/invalid output -> DISTINCT.
    for pair_id in expected:
        decisions.setdefault(
            pair_id,
            {
                "left_accepts_right": False,
                "right_accepts_left": False,
                "decision": (
                    "DISTINCT_FILTER"
                ),
            },
        )

    confirmed_edges = []

    for pair_id, result in (
        decisions.items()
    ):
        if (
            result["decision"]
            != "SAME_FILTER"
        ):
            continue

        pair = expected[pair_id]

        confirmed_edges.append(
            list(
                _pair_key(
                    pair[
                        "left_subtopic_id"
                    ],
                    pair[
                        "right_subtopic_id"
                    ],
                )
            )
        )

    return {
        "decisions": decisions,
        "confirmed_edges": (
            confirmed_edges
        ),
    }


def run_duplicate_filter_validation(
    *,
    client: OpenAI,
    model: str,
    prompt: str,
    topic_name: str,
    checkpoint_step: int,
    duplicate_candidates: list[dict],
    state: dict,
    event_by_id: dict[str, dict],
    temperature: float,
    max_tokens: int,
    json_mode: bool,
    max_retries: int,
    retry_wait: float,
) -> dict:
    if not duplicate_candidates:
        return {
            "status": "NO_CANDIDATES",
            "decisions": {},
            "confirmed_edges": [],
            "api_calls": 0,
            "usage": {},
            "elapsed_sec": 0.0,
            "warnings": [],
        }

    payload = (
        build_duplicate_validator_payload(
            topic_name=topic_name,
            checkpoint_step=(
                checkpoint_step
            ),
            duplicate_candidates=(
                duplicate_candidates
            ),
            state=state,
            event_by_id=event_by_id,
        )
    )

    try:
        call = run_json_call_with_retries(
            client=client,
            model=model,
            prompt=prompt,
            payload=payload,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
            max_retries=max_retries,
            retry_wait=retry_wait,
            validate_fn=lambda raw: (
                normalize_duplicate_validation_output(
                    raw,
                    duplicate_candidates=(
                        duplicate_candidates
                    ),
                )
            ),
        )

        return {
            "status": "SUCCESS",
            "decisions": call[
                "result"
            ]["decisions"],
            "confirmed_edges": call[
                "result"
            ]["confirmed_edges"],
            "api_calls": int(
                call.get(
                    "attempt",
                    1,
                )
            ),
            "usage": call["usage"],
            "elapsed_sec": call[
                "elapsed_sec"
            ],
            "warnings": [],
        }

    except Exception as exc:
        # merge는 보수적으로 포기한다.
        return {
            "status": (
                "FALLBACK_DISTINCT"
            ),
            "decisions": {
                item["pair_id"]: {
                    "left_accepts_right": False,
                    "right_accepts_left": False,
                    "decision": "DISTINCT_FILTER",
                }
                for item
                in duplicate_candidates
            },
            "confirmed_edges": [],
            "api_calls": max_retries,
            "usage": {},
            "elapsed_sec": 0.0,
            "warnings": [
                "Duplicate Validator 실패 -> "
                "모든 pair DISTINCT_FILTER: "
                f"{type(exc).__name__}: {exc}"
            ],
        }


def build_consolidation_groups(
    *,
    state: dict,
    confirmed_duplicate_edges: list[list[str]],
    contained_candidates: list[dict],
) -> dict:
    warnings = []

    subtopics = state.get(
        "subtopics",
        [],
    )
    by_id = {
        item["subtopic_id"]: item
        for item in subtopics
    }
    order = {
        item["subtopic_id"]: i
        for i, item in enumerate(
            subtopics
        )
    }

    duplicate_edges = {
        _pair_key(
            edge[0],
            edge[1],
        )
        for edge
        in confirmed_duplicate_edges
        if (
            isinstance(edge, list)
            and len(edge) >= 2
            and edge[0] in by_id
            and edge[1] in by_id
            and edge[0] != edge[1]
        )
    }

    # DUPLICATE는 clique만 허용.
    dup_groups = []

    for sid in [
        item["subtopic_id"]
        for item in subtopics
    ]:
        placed = False

        for group in dup_groups:
            if all(
                _pair_key(
                    sid,
                    member,
                )
                in duplicate_edges
                for member in group
            ):
                group.append(sid)
                placed = True
                break

        if not placed:
            dup_groups.append([sid])

    group_of = {}

    for gi, group in enumerate(
        dup_groups
    ):
        for sid in group:
            group_of[sid] = gi

    valid_cont = []

    for i, rel in enumerate(
        contained_candidates,
        start=1,
    ):
        child = rel.get(
            "child_subtopic_id"
        )
        container = rel.get(
            "container_subtopic_id"
        )

        if (
            child not in by_id
            or container not in by_id
            or child == container
        ):
            continue

        if (
            by_id[container].get(
                "type"
            )
            != "PROCESS"
        ):
            warnings.append(
                f"contained_by[{i}] container가 PROCESS 아님 -> 무시"
            )
            continue

        child_events = set(
            by_id[child].get(
                "event_ids",
                [],
            )
        )
        target_events = set(
            by_id[container].get(
                "event_ids",
                [],
            )
        )

        if (
            not child_events
            or not (
                child_events
                < target_events
            )
        ):
            warnings.append(
                f"contained_by[{i}] child Event가 container strict subset 아님 -> 무시"
            )
            continue

        child_group = group_of[
            child
        ]
        container_group = group_of[
            container
        ]

        if (
            child_group
            == container_group
        ):
            continue

        child_union = set()

        for sid in dup_groups[
            child_group
        ]:
            child_union.update(
                by_id[sid].get(
                    "event_ids",
                    [],
                )
            )

        if not child_union.issubset(
            target_events
        ):
            warnings.append(
                f"contained_by[{i}] duplicate child cluster 전체가 container에 포함되지 않음 -> 무시"
            )
            continue

        valid_cont.append({
            "child_group": (
                child_group
            ),
            "container_group": (
                container_group
            ),
            "container_id": (
                container
            ),
            "child_id": child,
        })

    # 한 child에 복수 container면 모두 포기.
    by_child = {}

    for rel in valid_cont:
        by_child.setdefault(
            rel["child_group"],
            [],
        ).append(rel)

    accepted = []

    for child_group, rels in (
        by_child.items()
    ):
        targets = {
            rel["container_group"]
            for rel in rels
        }

        if len(targets) != 1:
            warnings.append(
                "contained_by child에 복수 container -> 모두 무시"
            )
            continue

        accepted.append(
            sorted(
                rels,
                key=lambda rel: order[
                    rel["container_id"]
                ],
            )[0]
        )

    # containment chain 자동 전이 금지.
    child_groups = {
        rel["child_group"]
        for rel in accepted
    }

    root_rel = [
        rel
        for rel in accepted
        if rel[
            "container_group"
        ]
        not in child_groups
    ]

    if (
        len(accepted)
        != len(root_rel)
    ):
        warnings.append(
            "contained_by chain 자동 전이 병합 방지 -> 일부 무시"
        )

    attached = {}

    for rel in root_rel:
        attached.setdefault(
            rel["container_group"],
            [],
        ).append(rel)

    absorbed = {
        rel["child_group"]
        for rel in root_rel
    }

    groups = []
    idx = 1

    for gi, base_group in enumerate(
        dup_groups
    ):
        if gi in absorbed:
            continue

        source_ids = list(
            base_group
        )
        definition_source_id = min(
            base_group,
            key=lambda sid: order[
                sid
            ],
        )
        relation_mode = (
            "DUPLICATE"
            if len(base_group) > 1
            else "KEEP"
        )

        if gi in attached:
            rels = attached[gi]

            for rel in sorted(
                rels,
                key=lambda rel: order[
                    rel["child_id"]
                ],
            ):
                source_ids.extend(
                    dup_groups[
                        rel["child_group"]
                    ]
                )

            # PROCESS container 정의를 유지.
            definition_source_id = min(
                (
                    rel["container_id"]
                    for rel in rels
                ),
                key=lambda sid: order[
                    sid
                ],
            )
            relation_mode = (
                "CONTAINED_BY"
            )

        source_ids = sorted(
            dict.fromkeys(
                source_ids
            ),
            key=lambda sid: order[
                sid
            ],
        )

        definition = by_id[
            definition_source_id
        ]

        groups.append({
            "group_key": (
                f"G{idx:03d}"
            ),
            "source_subtopic_ids": (
                source_ids
            ),
            "definition_source_subtopic_id": (
                definition_source_id
            ),
            "relation_mode": (
                relation_mode
            ),
            "focal_basis": (
                definition[
                    "common_basis"
                ]
            ),
            "audit_criterion": (
                definition[
                    "membership_criterion"
                ]
            ),
        })

        idx += 1

    return {
        "groups": groups,
        "duplicate_edges": [
            list(edge)
            for edge in sorted(
                duplicate_edges
            )
        ],
        "contained_relations": [
            {
                "child_subtopic_id": (
                    rel["child_id"]
                ),
                "container_subtopic_id": (
                    rel[
                        "container_id"
                    ]
                ),
            }
            for rel in root_rel
        ],
        "warnings": warnings,
    }


def build_audit_payload(*,topic_name:str,checkpoint_step:int,group:dict,events:list[dict])->str:
    # periodic7: Consolidation Audit에는 Event Summary를 보내지 않는다.
    # Event core를 event_name으로 고정해 background contamination을 차단한다.
    return json.dumps(
        {
            "topic_name": topic_name,
            "checkpoint_step": checkpoint_step,
            "group_key": group["group_key"],
            "focal_basis": group["focal_basis"],
            "audit_criterion": group["audit_criterion"],
            "events": [
                {
                    "event_id": e["event_id"],
                    "event_name": e["event_name"],
                }
                for e in events
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


def normalize_audit_output(raw:Any,*,expected_event_ids:list[str])->dict:
    warnings=[]; allowed=set(expected_event_ids)
    if isinstance(raw,dict): decisions_raw=raw.get("decisions",[])
    elif isinstance(raw,list): decisions_raw=raw; warnings.append("audit top-level 배열을 decisions로 해석")
    else: decisions_raw=[]; warnings.append("audit 응답 형식 오류")
    if isinstance(decisions_raw,dict): decisions_raw=[decisions_raw]
    elif not isinstance(decisions_raw,list): decisions_raw=[]
    decisions={}
    for item in decisions_raw:
        if not isinstance(item,dict): continue
        ids=_extract_known_ids(item.get("event_id"),allowed_ids=allowed,object_keys=("event_id","event_ids","id"))
        if not ids: continue
        eid=ids[0]; d=_safe_text(item.get("decision")).upper()
        if d in {"REMOVE","DROP","EXCLUDE","NONE","NO"}: decision="REMOVE"
        elif d in {"KEEP","INCLUDE","DIRECT","RELATED","YES"}: decision="KEEP"
        else: warnings.append(f"{eid}: audit decision 불명확 ({d or 'EMPTY'})"); continue
        decisions.setdefault(eid,decision)
    missing=[eid for eid in expected_event_ids if eid not in decisions]
    return {"decisions":decisions,"missing_event_ids":missing,"warnings":warnings}


def run_consolidation_audit_tasks(*,client:OpenAI,model:str,prompt:str,topic_name:str,checkpoint_step:int,tasks:list[dict],event_by_id:dict[str,dict],temperature:float,max_tokens:int,json_mode:bool,max_retries:int,retry_wait:float,workers:int)->list[dict]:
    if not tasks: return []
    def one(task):
        ids=task["event_ids"]
        def call_for(target_ids,retries):
            payload=build_audit_payload(topic_name=topic_name,checkpoint_step=checkpoint_step,group=task["group"],events=[event_by_id[e] for e in target_ids])
            return run_json_call_with_retries(client=client,model=model,prompt=prompt,payload=payload,temperature=temperature,max_tokens=max_tokens,json_mode=json_mode,max_retries=retries,retry_wait=retry_wait,
                validate_fn=lambda raw:normalize_audit_output(raw,expected_event_ids=target_ids))
        try:
            first=call_for(ids,max_retries); decisions=dict(first["result"]["decisions"]); warnings=list(first["result"]["warnings"])
            missing=list(first["result"]["missing_event_ids"]); api_calls=int(first.get("attempt",1)); usage=first["usage"]; elapsed=first["elapsed_sec"]
            retry_ids=list(missing)
            if missing:
                try:
                    second=call_for(missing,1); api_calls+=int(second.get("attempt",1)); usage=_sum_usage(usage,second["usage"]); elapsed+=second["elapsed_sec"]
                    decisions.update(second["result"]["decisions"]); warnings.extend(second["result"]["warnings"])
                    missing=[e for e in missing if e not in decisions]
                except Exception as exc: warnings.append(f"누락 Event 재판정 실패: {type(exc).__name__}: {exc}")
            for e in missing: decisions[e]="KEEP"
            if missing: warnings.append("재판정 후에도 누락 -> KEEP fallback: "+", ".join(missing))
            return {"task":task,"result":{"decisions":decisions,"warnings":warnings},"attempt":first["attempt"],"elapsed_sec":round(elapsed,3),"usage":usage,
                    "status":"SUCCESS_WITH_MISSING_FALLBACK" if missing else "SUCCESS","api_calls":api_calls,"missing_retry_event_ids":retry_ids}
        except Exception as exc:
            return {"task":task,"result":{"decisions":{e:"KEEP" for e in ids},"warnings":[f"audit API/JSON 실패 -> chunk 전체 KEEP: {type(exc).__name__}: {exc}"]},
                    "attempt":max_retries,"elapsed_sec":0.0,"usage":{},"status":"FALLBACK_KEEP","api_calls":max_retries,"missing_retry_event_ids":[]}
    if workers<=1 or len(tasks)==1: return [one(t) for t in tasks]
    ordered=[None]*len(tasks)
    with ThreadPoolExecutor(max_workers=min(workers,len(tasks))) as ex:
        futures={ex.submit(one,t):i for i,t in enumerate(tasks)}
        for f in as_completed(futures): ordered[futures[f]]=f.result()
    return ordered


def build_label_payload(*,topic_name:str,checkpoint_step:int,groups:list[dict],event_by_id:dict[str,dict])->str:
    return json.dumps({"topic_name":topic_name,"checkpoint_step":checkpoint_step,
        "groups":[{"group_key":g["group_key"],"focal_basis":g["focal_basis"],"audit_criterion":g["audit_criterion"],
                   "events":[{"event_id":eid,"event_name":event_by_id[eid]["event_name"]} for eid in g["kept_event_ids"]]} for g in groups]},
        ensure_ascii=False,indent=2)


def normalize_label_output(
    raw: Any,
    *,
    groups: list[dict],
    state: dict,
) -> dict:
    """
    Label은 display-only.
    일부 name이 이상해도 전체 Label 요청을 실패시키지 않는다.
    """
    warnings = []

    group_by_key = {
        group["group_key"]: group
        for group in groups
    }
    by_id = {
        item["subtopic_id"]: item
        for item in state.get(
            "subtopics",
            [],
        )
    }

    if isinstance(raw, dict):
        labels_raw = raw.get(
            "labels",
            [],
        )
    elif isinstance(raw, list):
        labels_raw = raw
    else:
        labels_raw = []

    if isinstance(labels_raw, dict):
        labels_raw = [
            labels_raw
        ]

    if not isinstance(
        labels_raw,
        list,
    ):
        labels_raw = []

    raw_by_key = {}

    for item in labels_raw:
        if not isinstance(
            item,
            dict,
        ):
            continue

        key = _safe_text(
            item.get(
                "group_key"
            )
        )

        if key in group_by_key:
            raw_by_key[key] = item

    parsed = {}

    for group in groups:
        key = group["group_key"]
        source = by_id[
            group[
                "definition_source_subtopic_id"
            ]
        ]
        item = raw_by_key.get(
            key,
            {},
        )

        proposed = item.get(
            "name"
        )

        # 구버전 anchor_phrase도 호환.
        if proposed is None:
            proposed = item.get(
                "anchor_phrase"
            )

        name, status, name_warnings = (
            repair_display_name(
                proposed,
                common_basis=group[
                    "focal_basis"
                ],
                fallback_names=[
                    source.get(
                        "name"
                    )
                ],
            )
        )

        for warning in (
            name_warnings
        ):
            warnings.append(
                f"{key}: {warning}"
            )

        stype = _safe_text(
            item.get("type")
        ).upper()

        if (
            stype
            not in ALLOWED_SUBTOPIC_TYPES
        ):
            source_type = source.get(
                "type"
            )

            stype = (
                source_type
                if source_type
                in ALLOWED_SUBTOPIC_TYPES
                else "FOCAL_POINT"
            )

            warnings.append(
                f"{key}: type fallback -> {stype}"
            )

        parsed[key] = {
            "name": name,
            "display_name_status": (
                status
            ),
            "type": stype,
        }

    return {
        "labels": parsed,
        "warnings": warnings,
    }


def fallback_labels(
    *,
    groups: list[dict],
    state: dict,
) -> dict:
    by_id = {
        item["subtopic_id"]: item
        for item in state.get(
            "subtopics",
            [],
        )
    }
    labels = {}
    warnings = []

    for group in groups:
        source = by_id[
            group[
                "definition_source_subtopic_id"
            ]
        ]

        name, status, name_warnings = (
            repair_display_name(
                source.get("name"),
                common_basis=group[
                    "focal_basis"
                ],
            )
        )

        warnings.extend(
            f"{group['group_key']}: {warning}"
            for warning
            in name_warnings
        )

        stype = source.get(
            "type"
        )

        if (
            stype
            not in ALLOWED_SUBTOPIC_TYPES
        ):
            stype = "FOCAL_POINT"

        labels[
            group["group_key"]
        ] = {
            "name": name,
            "display_name_status": (
                status
            ),
            "type": stype,
        }

    return {
        "labels": labels,
        "warnings": warnings,
    }


def consolidation_due_reason(*,state:dict,min_events_for_subtopic:int,every:int,min_gap:int,new_subtopics_trigger:int)->str|None:
    if every<=0 or not state.get("subtopics"): return None
    current=int(state.get("last_processed_step",0)); last=int(state.get("last_consolidated_step",0)); baseline=max(last,min_events_for_subtopic)
    if current<=baseline: return None
    if current-baseline>=every: return "REGULAR_INTERVAL"
    if new_subtopics_trigger>0 and current-baseline>=min_gap:
        count=sum(1 for st in state.get("subtopics",[]) if int(st.get("created_at_step",0))>baseline)
        if count>=new_subtopics_trigger: return f"EARLY_NEW_SUBTOPICS:{count}"
    return None


def consolidation_due(*,state:dict,min_events_for_subtopic:int,every:int,min_gap:int=5,new_subtopics_trigger:int=3)->bool:
    return consolidation_due_reason(state=state,min_events_for_subtopic=min_events_for_subtopic,every=every,min_gap=min_gap,new_subtopics_trigger=new_subtopics_trigger) is not None


def run_periodic_consolidation(
    *,
    client: OpenAI,
    model: str,
    planner_prompt: str,
    duplicate_validator_prompt: str,
    audit_prompt: str,
    label_prompt: str,
    topic_name: str,
    state: dict,
    event_by_id: dict[str, dict],
    checkpoint_step: int,
    planner_temperature: float,
    duplicate_validator_temperature: float,
    audit_temperature: float,
    label_temperature: float,
    planner_max_tokens: int,
    duplicate_validator_max_tokens: int,
    audit_max_tokens: int,
    label_max_tokens: int,
    json_mode: bool,
    max_retries: int,
    retry_wait: float,
    audit_batch_size: int,
    workers: int,
    audit_log_path: Path,
    duplicate_log_path: Path,
) -> dict:
    before = copy.deepcopy(
        state.get(
            "subtopics",
            [],
        )
    )
    before_m = sum(
        len(
            item.get(
                "event_ids",
                [],
            )
        )
        for item in before
    )

    if not before:
        return {
            "status": "NO_SUBTOPICS",
            "state": state,
            "planner_calls": 0,
            "duplicate_validator_calls": 0,
            "audit_calls": 0,
            "label_calls": 0,
            "warnings": [],
            "before_subtopic_count": 0,
            "after_subtopic_count": 0,
            "before_membership_count": 0,
            "after_membership_count": 0,
            "merged_source_count": 0,
            "dropped_source_count": 0,
        }

    # -------------------------------------------------
    # Stage 1: relation proposal
    # -------------------------------------------------
    planner_payload = (
        build_consolidation_planner_payload(
            topic_name=topic_name,
            checkpoint_step=(
                checkpoint_step
            ),
            state=state,
            event_by_id=event_by_id,
        )
    )

    warnings = []

    try:
        planner_call = (
            run_json_call_with_retries(
                client=client,
                model=model,
                prompt=planner_prompt,
                payload=planner_payload,
                temperature=(
                    planner_temperature
                ),
                max_tokens=(
                    planner_max_tokens
                ),
                json_mode=json_mode,
                max_retries=(
                    max_retries
                ),
                retry_wait=retry_wait,
                validate_fn=lambda raw: (
                    normalize_consolidation_plan(
                        raw,
                        state=state,
                    )
                ),
            )
        )

        plan = planner_call[
            "result"
        ]
        warnings.extend(
            plan["warnings"]
        )
        planner_status = "SUCCESS"
        planner_calls = int(
            planner_call.get(
                "attempt",
                1,
            )
        )
        planner_usage = (
            planner_call["usage"]
        )
        planner_elapsed = (
            planner_call[
                "elapsed_sec"
            ]
        )

    except Exception as exc:
        ns = copy.deepcopy(
            state
        )
        ns[
            "last_consolidated_step"
        ] = checkpoint_step
        ns[
            "consolidation_count"
        ] = (
            int(
                ns.get(
                    "consolidation_count",
                    0,
                )
            )
            + 1
        )

        return {
            "status": (
                "PLANNER_FALLBACK_NO_CHANGE"
            ),
            "state": ns,
            "planner_calls": max_retries,
            "duplicate_validator_calls": 0,
            "audit_calls": 0,
            "label_calls": 0,
            "planner_status": "FALLBACK",
            "planner_usage": {},
            "planner_elapsed_sec": 0.0,
            "warnings": [
                "planner API/JSON 실패 -> canonical state 유지: "
                f"{type(exc).__name__}: {exc}"
            ],
            "before_subtopic_count": len(
                before
            ),
            "after_subtopic_count": len(
                before
            ),
            "before_membership_count": (
                before_m
            ),
            "after_membership_count": (
                before_m
            ),
            "merged_source_count": 0,
            "dropped_source_count": 0,
        }

    # -------------------------------------------------
    # Stage 1.5: second-stage duplicate validation
    # -------------------------------------------------
    duplicate_validation = (
        run_duplicate_filter_validation(
            client=client,
            model=model,
            prompt=(
                duplicate_validator_prompt
            ),
            topic_name=topic_name,
            checkpoint_step=(
                checkpoint_step
            ),
            duplicate_candidates=(
                plan[
                    "duplicate_candidates"
                ]
            ),
            state=state,
            event_by_id=event_by_id,
            temperature=(
                duplicate_validator_temperature
            ),
            max_tokens=(
                duplicate_validator_max_tokens
            ),
            json_mode=json_mode,
            max_retries=max_retries,
            retry_wait=retry_wait,
        )
    )

    warnings.extend(
        duplicate_validation[
            "warnings"
        ]
    )

    append_jsonl(
        duplicate_log_path,
        {
            "checkpoint_step": (
                checkpoint_step
            ),
            "status": (
                duplicate_validation[
                    "status"
                ]
            ),
            "candidates": (
                plan[
                    "duplicate_candidates"
                ]
            ),
            "decisions": (
                duplicate_validation[
                    "decisions"
                ]
            ),
            "confirmed_edges": (
                duplicate_validation[
                    "confirmed_edges"
                ]
            ),
            "api_calls": (
                duplicate_validation[
                    "api_calls"
                ]
            ),
            "usage": (
                duplicate_validation[
                    "usage"
                ]
            ),
            "elapsed_sec": (
                duplicate_validation[
                    "elapsed_sec"
                ]
            ),
            "model": model,
        },
    )

    group_result = (
        build_consolidation_groups(
            state=state,
            confirmed_duplicate_edges=(
                duplicate_validation[
                    "confirmed_edges"
                ]
            ),
            contained_candidates=(
                plan[
                    "contained_candidates"
                ]
            ),
        )
    )

    warnings.extend(
        group_result[
            "warnings"
        ]
    )

    groups = group_result[
        "groups"
    ]

    # -------------------------------------------------
    # Stage 2: frozen-criterion membership audit
    # -------------------------------------------------
    by_id = {
        item["subtopic_id"]: item
        for item in before
    }
    audit_tasks = []
    union_by_group = {}

    for group in groups:
        union = []

        for sid in group[
            "source_subtopic_ids"
        ]:
            for event_id in by_id[
                sid
            ].get(
                "event_ids",
                [],
            ):
                if (
                    event_id
                    not in union
                ):
                    union.append(
                        event_id
                    )

        union.sort(
            key=lambda event_id: (
                event_sort_key(
                    event_by_id[
                        event_id
                    ]
                )
            )
        )

        union_by_group[
            group["group_key"]
        ] = union

        for offset in range(
            0,
            len(union),
            audit_batch_size,
        ):
            audit_tasks.append({
                "group": group,
                "event_ids": union[
                    offset:
                    offset
                    + audit_batch_size
                ],
                "chunk_index": (
                    offset
                    // audit_batch_size
                    + 1
                ),
            })

    audit_results = (
        run_consolidation_audit_tasks(
            client=client,
            model=model,
            prompt=audit_prompt,
            topic_name=topic_name,
            checkpoint_step=(
                checkpoint_step
            ),
            tasks=audit_tasks,
            event_by_id=event_by_id,
            temperature=(
                audit_temperature
            ),
            max_tokens=(
                audit_max_tokens
            ),
            json_mode=json_mode,
            max_retries=max_retries,
            retry_wait=retry_wait,
            workers=workers,
        )
    )

    decisions = {
        group["group_key"]: {}
        for group in groups
    }
    audit_fallback = 0
    audit_calls = 0

    for result in audit_results:
        task = result["task"]
        key = task["group"][
            "group_key"
        ]

        decisions[key].update(
            result["result"][
                "decisions"
            ]
        )

        warnings.extend(
            result["result"][
                "warnings"
            ]
        )

        audit_calls += int(
            result.get(
                "api_calls",
                1,
            )
        )

        if (
            result["status"]
            != "SUCCESS"
        ):
            audit_fallback += 1

        append_jsonl(
            audit_log_path,
            {
                "checkpoint_step": (
                    checkpoint_step
                ),
                "group_key": key,
                "source_subtopic_ids": (
                    task["group"][
                        "source_subtopic_ids"
                    ]
                ),
                "definition_source_subtopic_id": (
                    task["group"][
                        "definition_source_subtopic_id"
                    ]
                ),
                "relation_mode": (
                    task["group"].get(
                        "relation_mode"
                    )
                ),
                "focal_basis": (
                    task["group"][
                        "focal_basis"
                    ]
                ),
                "audit_criterion": (
                    task["group"][
                        "audit_criterion"
                    ]
                ),
                "chunk_index": (
                    task["chunk_index"]
                ),
                "event_ids": (
                    task["event_ids"]
                ),
                "status": (
                    result["status"]
                ),
                "decisions": (
                    result["result"][
                        "decisions"
                    ]
                ),
                "missing_retry_event_ids": (
                    result.get(
                        "missing_retry_event_ids",
                        [],
                    )
                ),
                "api_calls": (
                    result.get(
                        "api_calls",
                        1,
                    )
                ),
                "warnings": (
                    result["result"][
                        "warnings"
                    ]
                ),
                "attempt": (
                    result["attempt"]
                ),
                "elapsed_sec": (
                    result[
                        "elapsed_sec"
                    ]
                ),
                "usage": (
                    result["usage"]
                ),
                "model": model,
            },
        )

    audited = []
    dropped = []

    for group in groups:
        union = union_by_group[
            group["group_key"]
        ]
        group_decisions = (
            decisions[
                group["group_key"]
            ]
        )

        kept = [
            event_id
            for event_id in union
            if group_decisions.get(
                event_id,
                "KEEP",
            )
            == "KEEP"
        ]
        removed = [
            event_id
            for event_id in union
            if event_id not in set(
                kept
            )
        ]

        enriched = {
            **group,
            "union_event_ids": union,
            "kept_event_ids": kept,
            "removed_event_ids": (
                removed
            ),
        }

        (
            dropped
            if len(kept) < 2
            else audited
        ).append(
            enriched
        )

    # -------------------------------------------------
    # Stage 3: display-name/type only
    # -------------------------------------------------
    label_calls = 0
    label_usage = {}
    label_elapsed = 0.0

    if audited:
        payload = build_label_payload(
            topic_name=topic_name,
            checkpoint_step=(
                checkpoint_step
            ),
            groups=audited,
            event_by_id=event_by_id,
        )

        try:
            label_call = (
                run_json_call_with_retries(
                    client=client,
                    model=model,
                    prompt=label_prompt,
                    payload=payload,
                    temperature=(
                        label_temperature
                    ),
                    max_tokens=(
                        label_max_tokens
                    ),
                    json_mode=json_mode,
                    max_retries=(
                        max_retries
                    ),
                    retry_wait=(
                        retry_wait
                    ),
                    validate_fn=lambda raw: (
                        normalize_label_output(
                            raw,
                            groups=audited,
                            state=state,
                        )
                    ),
                )
            )

            labels = label_call[
                "result"
            ]
            warnings.extend(
                labels["warnings"]
            )
            label_calls = int(
                label_call.get(
                    "attempt",
                    1,
                )
            )
            label_usage = (
                label_call["usage"]
            )
            label_elapsed = (
                label_call[
                    "elapsed_sec"
                ]
            )
            label_status = "SUCCESS"

        except Exception as exc:
            labels = fallback_labels(
                groups=audited,
                state=state,
            )
            warnings.extend(
                labels["warnings"]
            )
            warnings.append(
                "label API 실패 -> display-name fallback: "
                f"{type(exc).__name__}: {exc}"
            )
            label_status = "FALLBACK"

    else:
        labels = {
            "labels": {},
            "warnings": [],
        }
        label_status = (
            "NO_SURVIVORS"
        )

    # -------------------------------------------------
    # Stage 4: canonical state apply
    # -------------------------------------------------
    source_order = {
        item["subtopic_id"]: i
        for i, item in enumerate(
            before
        )
    }
    new_active = []
    retired = []
    merged = 0
    dropped_count = 0

    for group in audited:
        source_ids = sorted(
            group[
                "source_subtopic_ids"
            ],
            key=lambda sid: (
                source_order[sid]
            ),
        )
        canonical_id = group[
            "definition_source_subtopic_id"
        ]
        source = by_id[
            canonical_id
        ]
        label = labels[
            "labels"
        ][
            group["group_key"]
        ]

        item = copy.deepcopy(
            source
        )
        item[
            "subtopic_id"
        ] = canonical_id
        item["name"] = label[
            "name"
        ]
        item[
            "display_name_status"
        ] = label.get(
            "display_name_status",
            "OK",
        )
        item["type"] = label[
            "type"
        ]

        # Audit 기준은 Label이 바꾸지 못한다.
        item["common_basis"] = (
            group[
                "focal_basis"
            ]
        )
        item[
            "membership_criterion"
        ] = group[
            "audit_criterion"
        ]
        item["event_ids"] = list(
            group[
                "kept_event_ids"
            ]
        )

        item[
            "creation_membership_validations"
        ] = []
        item[
            "update_history"
        ] = []
        item[
            "refinement_history"
        ] = []
        item[
            "creation_scope_refinement"
        ] = None

        history = list(
            item.get(
                "consolidation_history",
                [],
            )
        )

        history.append({
            "checkpoint_step": (
                checkpoint_step
            ),
            "source_subtopic_ids": (
                source_ids
            ),
            "definition_source_subtopic_id": (
                canonical_id
            ),
            "relation_mode": (
                group.get(
                    "relation_mode"
                )
            ),
            "source_names": [
                by_id[sid]["name"]
                for sid in source_ids
            ],
            "frozen_common_basis": (
                group[
                    "focal_basis"
                ]
            ),
            "frozen_membership_criterion": (
                group[
                    "audit_criterion"
                ]
            ),
            "union_event_ids": (
                group[
                    "union_event_ids"
                ]
            ),
            "kept_event_ids": (
                group[
                    "kept_event_ids"
                ]
            ),
            "removed_event_ids": (
                group[
                    "removed_event_ids"
                ]
            ),
            "action": (
                "KEEP"
                if len(source_ids) == 1
                else "MERGE"
            ),
        })

        item[
            "consolidation_history"
        ] = history
        item[
            "canonical_source_subtopic_ids"
        ] = source_ids
        item[
            "definition_source_subtopic_id"
        ] = canonical_id

        new_active.append(
            item
        )

        for retired_id in source_ids:
            if retired_id == canonical_id:
                continue

            merged += 1

            retired.append({
                "subtopic_id": (
                    retired_id
                ),
                "status": "MERGED",
                "merged_into": (
                    canonical_id
                ),
                "checkpoint_step": (
                    checkpoint_step
                ),
                "previous_name": (
                    by_id[
                        retired_id
                    ]["name"]
                ),
                "relation_mode": (
                    group.get(
                        "relation_mode"
                    )
                ),
            })

    for group in dropped:
        for sid in group[
            "source_subtopic_ids"
        ]:
            dropped_count += 1

            retired.append({
                "subtopic_id": sid,
                "status": "DROPPED",
                "merged_into": None,
                "checkpoint_step": (
                    checkpoint_step
                ),
                "previous_name": (
                    by_id[sid]["name"]
                ),
                "reason": (
                    "Membership Audit 후 KEEP Event가 2개 미만"
                ),
                "kept_event_ids": (
                    group[
                        "kept_event_ids"
                    ]
                ),
                "removed_event_ids": (
                    group[
                        "removed_event_ids"
                    ]
                ),
            })

    new_active.sort(
        key=lambda item: (
            source_order.get(
                item["subtopic_id"],
                10**9,
            )
        )
    )

    ns = copy.deepcopy(
        state
    )
    ns["subtopics"] = (
        new_active
    )
    ns.setdefault(
        "retired_subtopics",
        [],
    ).extend(
        retired
    )
    ns[
        "last_consolidated_step"
    ] = checkpoint_step
    ns[
        "consolidation_count"
    ] = (
        int(
            ns.get(
                "consolidation_count",
                0,
            )
        )
        + 1
    )

    after_m = sum(
        len(
            item.get(
                "event_ids",
                [],
            )
        )
        for item in new_active
    )

    return {
        "status": (
            "SUCCESS_WITH_AUDIT_FALLBACK"
            if audit_fallback
            else (
                "SUCCESS_WITH_LABEL_FALLBACK"
                if label_status
                == "FALLBACK"
                else "SUCCESS"
            )
        ),
        "state": ns,
        "planner_calls": (
            planner_calls
        ),
        "duplicate_validator_calls": (
            duplicate_validation[
                "api_calls"
            ]
        ),
        "duplicate_validator_status": (
            duplicate_validation[
                "status"
            ]
        ),
        "audit_calls": (
            audit_calls
        ),
        "label_calls": (
            label_calls
        ),
        "planner_status": (
            planner_status
        ),
        "label_status": (
            label_status
        ),
        "audit_fallback_count": (
            audit_fallback
        ),
        "planner_usage": (
            planner_usage
        ),
        "planner_elapsed_sec": (
            planner_elapsed
        ),
        "duplicate_validator_usage": (
            duplicate_validation[
                "usage"
            ]
        ),
        "duplicate_validator_elapsed_sec": (
            duplicate_validation[
                "elapsed_sec"
            ]
        ),
        "label_usage": (
            label_usage
        ),
        "label_elapsed_sec": (
            label_elapsed
        ),
        "warnings": (
            warnings
        ),
        "planner_duplicate_candidates": (
            plan[
                "duplicate_candidates"
            ]
        ),
        "duplicate_edges": (
            group_result[
                "duplicate_edges"
            ]
        ),
        "contained_relations": (
            group_result[
                "contained_relations"
            ]
        ),
        "before_subtopic_count": len(
            before
        ),
        "after_subtopic_count": len(
            new_active
        ),
        "before_membership_count": (
            before_m
        ),
        "after_membership_count": (
            after_m
        ),
        "merged_source_count": (
            merged
        ),
        "dropped_source_count": (
            dropped_count
        ),
        "groups": [
            {
                "group_key": (
                    group[
                        "group_key"
                    ]
                ),
                "source_subtopic_ids": (
                    group[
                        "source_subtopic_ids"
                    ]
                ),
                "definition_source_subtopic_id": (
                    group[
                        "definition_source_subtopic_id"
                    ]
                ),
                "relation_mode": (
                    group.get(
                        "relation_mode"
                    )
                ),
                "focal_basis": (
                    group[
                        "focal_basis"
                    ]
                ),
                "audit_criterion": (
                    group[
                        "audit_criterion"
                    ]
                ),
                "union_event_ids": (
                    group[
                        "union_event_ids"
                    ]
                ),
                "kept_event_ids": (
                    group[
                        "kept_event_ids"
                    ]
                ),
                "removed_event_ids": (
                    group[
                        "removed_event_ids"
                    ]
                ),
                "survived": True,
            }
            for group in audited
        ] + [
            {
                "group_key": (
                    group[
                        "group_key"
                    ]
                ),
                "source_subtopic_ids": (
                    group[
                        "source_subtopic_ids"
                    ]
                ),
                "definition_source_subtopic_id": (
                    group[
                        "definition_source_subtopic_id"
                    ]
                ),
                "relation_mode": (
                    group.get(
                        "relation_mode"
                    )
                ),
                "focal_basis": (
                    group[
                        "focal_basis"
                    ]
                ),
                "audit_criterion": (
                    group[
                        "audit_criterion"
                    ]
                ),
                "union_event_ids": (
                    group[
                        "union_event_ids"
                    ]
                ),
                "kept_event_ids": (
                    group[
                        "kept_event_ids"
                    ]
                ),
                "removed_event_ids": (
                    group[
                        "removed_event_ids"
                    ]
                ),
                "survived": False,
            }
            for group in dropped
        ],
        "retired_entries": retired,
    }


def execute_consolidation_checkpoint(*,client:OpenAI,model:str,planner_prompt:str,duplicate_validator_prompt:str,audit_prompt:str,label_prompt:str,topic_name:str,state:dict,event_by_id:dict[str,dict],checkpoint_step:int,trigger_reason:str,
                                     planner_temperature:float,duplicate_validator_temperature:float,audit_temperature:float,label_temperature:float,planner_max_tokens:int,duplicate_validator_max_tokens:int,audit_max_tokens:int,label_max_tokens:int,json_mode:bool,max_retries:int,retry_wait:float,
                                     audit_batch_size:int,workers:int,state_path:Path,history_path:Path,consolidation_log_path:Path,audit_log_path:Path,duplicate_log_path:Path)->tuple[dict,dict]:
    print(); print(f"  [Consolidation] checkpoint step={checkpoint_step} | trigger={trigger_reason}")
    result=run_periodic_consolidation(client=client,model=model,planner_prompt=planner_prompt,duplicate_validator_prompt=duplicate_validator_prompt,audit_prompt=audit_prompt,label_prompt=label_prompt,topic_name=topic_name,state=state,event_by_id=event_by_id,
        checkpoint_step=checkpoint_step,
        planner_temperature=planner_temperature,
        duplicate_validator_temperature=duplicate_validator_temperature,
        audit_temperature=audit_temperature,
        label_temperature=label_temperature,
        planner_max_tokens=planner_max_tokens,
        duplicate_validator_max_tokens=duplicate_validator_max_tokens,
        audit_max_tokens=audit_max_tokens,
        label_max_tokens=label_max_tokens,
        json_mode=json_mode,
        max_retries=max_retries,
        retry_wait=retry_wait,
        audit_batch_size=audit_batch_size,
        workers=workers,
        audit_log_path=audit_log_path,
        duplicate_log_path=duplicate_log_path)
    ns=result["state"]; save_json_atomic(state_path,ns)
    row={k:v for k,v in result.items() if k!="state"}; row.update({"checkpoint_step":checkpoint_step,"trigger_reason":trigger_reason,"model":model,"consolidation_version":CONSOLIDATION_VERSION}); append_jsonl(consolidation_log_path,row)
    append_jsonl(history_path,{"kind":"CONSOLIDATION","step":checkpoint_step,"trigger_reason":trigger_reason,"subtopic_count":len(ns["subtopics"]),"state":ns,
        "consolidation_summary":{"status":result["status"],"before_subtopic_count":result["before_subtopic_count"],"after_subtopic_count":result["after_subtopic_count"],"before_membership_count":result["before_membership_count"],"after_membership_count":result["after_membership_count"],"merged_source_count":result["merged_source_count"],"dropped_source_count":result["dropped_source_count"]}})
    print(f"  -> {result['before_subtopic_count']} Subtopics / {result['before_membership_count']} memberships → {result['after_subtopic_count']} / {result['after_membership_count']}")
    print(
        "  -> API calls | "
        f"planner {result['planner_calls']} | "
        f"duplicate-validator {result.get('duplicate_validator_calls', 0)} | "
        f"audit {result['audit_calls']} | "
        f"label {result['label_calls']}"
    )
    print(f"  -> retired | MERGED {result['merged_source_count']} | DROPPED {result['dropped_source_count']}")
    print(f"  -> status: {result['status']}")
    if result["warnings"]: print(f"  -> warnings: {len(result['warnings'])}건 (consolidation_runs.jsonl 확인)")
    return ns,result


# ---------------------------------------------------------
# Main
# ---------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Subtopic 후보 생성과 원자 requirement 기반 독립 Membership Validation을 "
            "분리한 스트리밍 시뮬레이터 v1.8.8-periodic7"
        )
    )

    parser.add_argument("--input", required=True)
    parser.add_argument("--topic-name", required=True)
    parser.add_argument("--output-dir", required=True)

    parser.add_argument(
        "--candidate-prompt",
        default="17_77_subtopic_candidate_prompt_v1_8_8_periodic7.md",
    )
    parser.add_argument(
        "--validator-prompt",
        default="17_77_membership_validator_prompt_v1_8_8_periodic7.md",
    )
    parser.add_argument(
        "--candidate-schema",
        default="17_77_subtopic_candidate_schema_v1_8_8_periodic7.json",
    )
    parser.add_argument(
        "--validator-schema",
        default="17_77_membership_validator_schema_v1_8_8_periodic7.json",
    )
    parser.add_argument(
        "--consolidation-planner-prompt",
        default="17_77_consolidation_planner_prompt_v1_8_8_periodic7.md",
    )
    parser.add_argument(
        "--duplicate-validator-prompt",
        default="17_77_duplicate_filter_validator_prompt_v1_8_8_periodic7.md",
    )
    parser.add_argument(
        "--consolidation-audit-prompt",
        default="17_77_consolidation_membership_audit_prompt_v1_8_8_periodic7.md",
    )
    parser.add_argument(
        "--consolidation-label-prompt",
        default="17_77_consolidation_label_prompt_v1_8_8_periodic7.md",
    )
    parser.add_argument(
        "--consolidation-planner-schema",
        default="17_77_consolidation_planner_schema_v1_8_8_periodic7.json",
    )
    parser.add_argument(
        "--duplicate-validator-schema",
        default="17_77_duplicate_filter_validator_schema_v1_8_8_periodic7.json",
    )
    parser.add_argument(
        "--consolidation-audit-schema",
        default="17_77_consolidation_membership_audit_schema_v1_8_8_periodic7.json",
    )
    parser.add_argument(
        "--consolidation-label-schema",
        default="17_77_consolidation_label_schema_v1_8_8_periodic7.json",
    )

    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--model", default=None)

    parser.add_argument(
        "--candidate-temperature",
        type=float,
        default=0.0,
    )
    parser.add_argument(
        "--validator-temperature",
        type=float,
        default=0.0,
    )
    parser.add_argument(
        "--consolidation-planner-temperature",
        type=float,
        default=0.0,
    )
    parser.add_argument(
        "--duplicate-validator-temperature",
        type=float,
        default=0.0,
    )
    parser.add_argument(
        "--consolidation-audit-temperature",
        type=float,
        default=0.0,
    )
    parser.add_argument(
        "--consolidation-label-temperature",
        type=float,
        default=0.0,
    )
    parser.add_argument(
        "--candidate-max-tokens",
        type=int,
        default=1800,
    )
    parser.add_argument(
        "--validator-max-tokens",
        type=int,
        default=800,
    )
    parser.add_argument(
        "--consolidation-planner-max-tokens",
        type=int,
        default=1000,
    )
    parser.add_argument(
        "--duplicate-validator-max-tokens",
        type=int,
        default=900,
    )
    parser.add_argument(
        "--consolidation-audit-max-tokens",
        type=int,
        default=1000,
    )
    parser.add_argument(
        "--consolidation-label-max-tokens",
        type=int,
        default=700,
    )
    parser.add_argument(
        "--consolidation-max-retries",
        type=int,
        default=2,
        help="Consolidation 각 API 호출 최대 시도 수 (기본 2)",
    )

    parser.add_argument("--json-mode", action="store_true")
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument("--retry-wait", type=float, default=3.0)
    parser.add_argument("--sleep", type=float, default=0.0)
    parser.add_argument(
        "--validator-workers",
        type=int,
        default=6,
        help="독립 Membership Validator 병렬 요청 수 (기본 6)",
    )
    parser.add_argument(
        "--min-new-keep",
        type=int,
        default=2,
        help="신규 Subtopic 생성에 필요한 KEEP Event 최소 수 (기본 2)",
    )
    parser.add_argument(
        "--consolidation-every",
        type=int,
        default=10,
        help=(
            "첫 Subtopic 임계치 이후 몇 Event마다 canonical "
            "Consolidation을 실행할지 (기본 10)"
        ),
    )
    parser.add_argument(
        "--consolidation-min-gap",
        type=int,
        default=5,
        help="조기 Consolidation 최소 Event 간격 (기본 5)",
    )
    parser.add_argument(
        "--consolidation-new-subtopics-trigger",
        type=int,
        default=3,
        help=(
            "최소 간격 이후 신규 active Subtopic이 몇 개 쌓이면 "
            "조기 Consolidation할지 (기본 3, 0이면 OFF)"
        ),
    )
    parser.add_argument(
        "--consolidation-audit-batch-size",
        type=int,
        default=10,
        help="Membership Audit 한 요청당 Event 수 (기본 10)",
    )
    parser.add_argument(
        "--consolidation-workers",
        type=int,
        default=6,
        help="Consolidation Audit 병렬 요청 수 (기본 6)",
    )
    parser.add_argument(
        "--stop-on-event-error",
        action="store_true",
        help=(
            "Event 처리 API/JSON 오류 시 즉시 중단. "
            "기본은 Event를 processed-only fallback으로 기록하고 계속 진행"
        ),
    )
    parser.add_argument(
        "--stop-on-consolidation-error",
        action="store_true",
        help=(
            "Consolidation 코드/예외 발생 시 즉시 중단. "
            "기본은 현재 canonical state를 유지하고 다음 Event로 진행"
        ),
    )
    parser.add_argument("--limit", type=int, default=None)

    parser.add_argument(
        "--min-events-for-subtopic",
        type=int,
        default=7,
    )

    parser.add_argument("--reset", action="store_true")
    parser.add_argument(
        "--consolidation-only",
        action="store_true",
        help=(
            "Streaming은 실행하지 않고 기존 canonical state에 "
            "Consolidation checkpoint를 1회 즉시 실행"
        ),
    )
    parser.add_argument(
        "--disable-consolidation",
        action="store_true",
        help="Streaming은 실행하되 periodic Consolidation은 끔",
    )

    args = parser.parse_args()

    if args.validator_workers < 1:
        parser.error("--validator-workers는 1 이상이어야 합니다.")

    if args.min_new_keep < 2:
        parser.error("--min-new-keep은 2 이상이어야 합니다.")

    if args.consolidation_max_retries < 1:
        parser.error(
            "--consolidation-max-retries는 1 이상이어야 합니다."
        )

    if args.consolidation_every < 1:
        parser.error("--consolidation-every는 1 이상이어야 합니다.")

    if args.consolidation_min_gap < 1:
        parser.error("--consolidation-min-gap은 1 이상이어야 합니다.")

    if args.consolidation_new_subtopics_trigger < 0:
        parser.error(
            "--consolidation-new-subtopics-trigger는 0 이상이어야 합니다."
        )

    if args.consolidation_audit_batch_size < 1:
        parser.error(
            "--consolidation-audit-batch-size는 1 이상이어야 합니다."
        )

    if args.consolidation_workers < 1:
        parser.error(
            "--consolidation-workers는 1 이상이어야 합니다."
        )

    if args.consolidation_only and args.reset:
        parser.error(
            "--consolidation-only와 --reset은 함께 사용할 수 없습니다."
        )

    if args.consolidation_only and args.disable_consolidation:
        parser.error(
            "--consolidation-only와 --disable-consolidation은 "
            "함께 사용할 수 없습니다."
        )

    input_path = Path(args.input)
    output_dir = Path(args.output_dir)
    candidate_prompt_path = Path(args.candidate_prompt)
    validator_prompt_path = Path(args.validator_prompt)
    candidate_schema_path = Path(args.candidate_schema)
    validator_schema_path = Path(args.validator_schema)
    consolidation_planner_prompt_path = Path(
        args.consolidation_planner_prompt
    )
    duplicate_validator_prompt_path = Path(
        args.duplicate_validator_prompt
    )
    consolidation_audit_prompt_path = Path(
        args.consolidation_audit_prompt
    )
    consolidation_label_prompt_path = Path(
        args.consolidation_label_prompt
    )
    consolidation_planner_schema_path = Path(
        args.consolidation_planner_schema
    )
    duplicate_validator_schema_path = Path(
        args.duplicate_validator_schema
    )
    consolidation_audit_schema_path = Path(
        args.consolidation_audit_schema
    )
    consolidation_label_schema_path = Path(
        args.consolidation_label_schema
    )
    env_path = Path(args.env_file)

    for path, label in [
        (input_path, "input"),
        (candidate_prompt_path, "candidate prompt"),
        (validator_prompt_path, "validator prompt"),
        (candidate_schema_path, "candidate schema"),
        (validator_schema_path, "validator schema"),
        (
            consolidation_planner_prompt_path,
            "consolidation planner prompt",
        ),
        (
            duplicate_validator_prompt_path,
            "duplicate validator prompt",
        ),
        (
            consolidation_audit_prompt_path,
            "consolidation audit prompt",
        ),
        (
            consolidation_label_prompt_path,
            "consolidation label prompt",
        ),
        (
            consolidation_planner_schema_path,
            "consolidation planner schema",
        ),
        (
            duplicate_validator_schema_path,
            "duplicate validator schema",
        ),
        (
            consolidation_audit_schema_path,
            "consolidation audit schema",
        ),
        (
            consolidation_label_schema_path,
            "consolidation label schema",
        ),
        (env_path, ".env"),
    ]:
        if not path.exists():
            parser.error(f"{label} 파일을 찾을 수 없습니다: {path}")

    # 문법/파일 존재 확인용
    load_json(candidate_schema_path)
    load_json(validator_schema_path)
    load_json(consolidation_planner_schema_path)
    load_json(duplicate_validator_schema_path)
    load_json(consolidation_audit_schema_path)
    load_json(consolidation_label_schema_path)

    if args.reset:
        reset_output_dir(output_dir)
    else:
        output_dir.mkdir(parents=True, exist_ok=True)

    state_path = output_dir / "subtopic_state.json"
    decisions_path = output_dir / "decisions.jsonl"
    history_path = output_dir / "state_history.jsonl"
    failures_path = output_dir / "failures.jsonl"
    membership_log_path = output_dir / "membership_validations.jsonl"
    consolidation_log_path = (
        output_dir / "consolidation_runs.jsonl"
    )
    consolidation_audit_log_path = (
        output_dir / "consolidation_membership_audits.jsonl"
    )
    duplicate_validation_log_path = (
        output_dir / "duplicate_filter_validations.jsonl"
    )

    load_dotenv(dotenv_path=env_path, override=False)

    api_key = (
        os.getenv("QWEN_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or os.getenv("OPENROUTER_API_KEY")
    )
    base_url = (
        os.getenv("QWEN_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or os.getenv("OPENROUTER_BASE_URL")
    )

    if not api_key:
        parser.error(".env에서 API key를 찾지 못했습니다.")

    if not base_url:
        parser.error(".env에서 API base URL을 찾지 못했습니다.")

    model = (
        args.model
        or os.getenv("OPENROUTER_SUBTOPIC_MODEL")
        or os.getenv("QWEN_MODEL")
        or DEFAULT_MODEL
    )

    candidate_prompt = load_text(candidate_prompt_path)
    validator_prompt = load_text(validator_prompt_path)
    consolidation_planner_prompt = load_text(
        consolidation_planner_prompt_path
    )
    duplicate_validator_prompt = load_text(
        duplicate_validator_prompt_path
    )
    consolidation_audit_prompt = load_text(
        consolidation_audit_prompt_path
    )
    consolidation_label_prompt = load_text(
        consolidation_label_prompt_path
    )

    events = load_jsonl(input_path)

    required_fields = {
        "event_id",
        "event_name",
        "representative_published_date",
        "first_published_at",
        "event_summary",
        "anchors",
    }

    for row in events:
        missing = required_fields - set(row)

        if missing:
            parser.error(
                f"{row.get('event_id', '?')} 필수 필드 누락: "
                + ", ".join(sorted(missing))
            )

    events.sort(key=event_sort_key)

    ids = [item["event_id"] for item in events]

    if len(ids) != len(set(ids)):
        parser.error("input에 중복 event_id가 있습니다.")

    event_by_id = {
        item["event_id"]: item
        for item in events
    }

    if state_path.exists():
        state = load_json(state_path)

        if state.get("topic_name") != args.topic_name:
            parser.error(
                "기존 state topic_name과 --topic-name이 다릅니다. "
                "--reset 또는 새 output-dir을 사용하세요."
            )

        state = ensure_consolidation_state_fields(state)

        for subtopic in state.get("subtopics", []):
            if not subtopic.get("common_basis"):
                parser.error(
                    "기존 state Subtopic에 common_basis가 없습니다. "
                    "v1.8은 새 output-dir 사용을 권장합니다."
                )

            criterion = subtopic.get("membership_criterion")

            if not isinstance(criterion, str) or not criterion.strip():
                parser.error(
                    "기존 state Subtopic에 membership_criterion이 없습니다. "
                    "v1.8은 반드시 새 output-dir을 사용하세요."
                )
    else:
        if args.consolidation_only:
            parser.error(
                "--consolidation-only을 사용하려면 output-dir에 "
                "기존 subtopic_state.json이 있어야 합니다."
            )

        state = initial_state(args.topic_name)
        save_json_atomic(state_path, state)

    processed = get_processed_event_ids(state)

    unknown_processed = processed - set(ids)

    if unknown_processed:
        parser.error(
            "state에 input에 없는 processed_event_id가 있습니다: "
            + ", ".join(sorted(unknown_processed))
        )

    remaining = [
        item
        for item in events
        if item["event_id"] not in processed
    ]

    if args.consolidation_only:
        remaining = []
    elif args.limit is not None:
        remaining = remaining[:args.limit]

    client = OpenAI(api_key=api_key, base_url=base_url)

    print("=" * 78)
    print("Streaming Subtopic v1.8.8-periodic4 - Display-only Names + Duplicate Validation")
    print(f"- topic_name       : {args.topic_name}")
    print(f"- input            : {input_path}")
    print(f"- output_dir       : {output_dir}")
    print(f"- model            : {model}")
    print(f"- 전체 Event        : {len(events)}")
    print(f"- 기존 처리         : {len(processed)}")
    print(f"- 이번 실행         : {len(remaining)}")
    print(f"- 기존 Subtopic     : {len(state['subtopics'])}")
    print(f"- 생성 임계치       : {args.min_events_for_subtopic} Events")
    print(f"- Validator workers : {args.validator_workers}")
    print(f"- 신규 KEEP 최소    : {args.min_new_keep}")
    print("- Candidate context : current full + past event_name index")
    print("- Streaming Refiner : OFF")
    print(
        "- Consolidation      : "
        + (
            "ONLY"
            if args.consolidation_only
            else "OFF"
            if args.disable_consolidation
            else (
                f"ON / every {args.consolidation_every} Events"
            )
        )
    )
    print(
        "- Consolidation audit: "
        f"batch {args.consolidation_audit_batch_size} / "
        f"workers {args.consolidation_workers}"
    )
    print(
        "- Early trigger       : "
        f"min-gap {args.consolidation_min_gap} / "
        f"new-subtopics {args.consolidation_new_subtopics_trigger}"
    )
    print(
        "- Error policy        : "
        + (
            "STOP event"
            if args.stop_on_event_error
            else "CONTINUE event"
        )
        + " / "
        + (
            "STOP consolidation"
            if args.stop_on_consolidation_error
            else "CONTINUE consolidation"
        )
    )
    print("=" * 78)

    if not remaining:
        print("Streaming에서 처리할 Event가 없습니다.")

    if args.consolidation_only:
        if not state.get("subtopics"):
            print(
                "Consolidation할 활성 Subtopic이 없습니다."
            )
        else:
            state, _ = execute_consolidation_checkpoint(
                client=client,
                model=model,
                planner_prompt=consolidation_planner_prompt,
                duplicate_validator_prompt=duplicate_validator_prompt,
                audit_prompt=consolidation_audit_prompt,
                label_prompt=consolidation_label_prompt,
                topic_name=args.topic_name,
                state=state,
                event_by_id=event_by_id,
                checkpoint_step=state.get(
                    "last_processed_step",
                    0,
                ),
                trigger_reason="MANUAL",
                planner_temperature=(
                    args.consolidation_planner_temperature
                ),
                duplicate_validator_temperature=(
                    args.duplicate_validator_temperature
                ),
                audit_temperature=(
                    args.consolidation_audit_temperature
                ),
                label_temperature=(
                    args.consolidation_label_temperature
                ),
                planner_max_tokens=(
                    args.consolidation_planner_max_tokens
                ),
                duplicate_validator_max_tokens=(
                    args.duplicate_validator_max_tokens
                ),
                audit_max_tokens=(
                    args.consolidation_audit_max_tokens
                ),
                label_max_tokens=(
                    args.consolidation_label_max_tokens
                ),
                json_mode=args.json_mode,
                max_retries=(
                    args.consolidation_max_retries
                ),
                retry_wait=args.retry_wait,
                audit_batch_size=(
                    args.consolidation_audit_batch_size
                ),
                workers=args.consolidation_workers,
                state_path=state_path,
                history_path=history_path,
                consolidation_log_path=(
                    consolidation_log_path
                ),
                audit_log_path=(
                    consolidation_audit_log_path
                ),
                duplicate_log_path=(
                    duplicate_validation_log_path
                ),
            )

    for run_index, current_event in enumerate(remaining, start=1):
        past_events = [
            event_by_id[event_id]
            for event_id in state["processed_event_ids"]
        ]

        available_event_ids = {
            item["event_id"]
            for item in past_events
        }
        available_event_ids.add(current_event["event_id"])

        step_number = state.get("last_processed_step", 0) + 1

        print(
            f"[{run_index}/{len(remaining)}] "
            f"step={step_number} | "
            f"{current_event['event_id']} | "
            f"{current_event['event_name']}"
        )

        if len(available_event_ids) < args.min_events_for_subtopic:
            final_decision = {
                "current_event_id": current_event["event_id"],
                "existing_subtopic_updates": [],
                "new_subtopics": [],
                "dropped_new_candidates": [],
                "new_subtopic_overlap_report": [],
                "validator_call_count": 0,
                "refiner_call_count": 0,
            }

            state = apply_final_decision(
                state=state,
                final_decision=final_decision,
                current_event=current_event,
                step_number=step_number,
            )

            append_jsonl(
                decisions_path,
                {
                    "step": step_number,
                    "topic_name": args.topic_name,
                    "current_event_id": current_event["event_id"],
                    "auto_rule": "BELOW_SUBTOPIC_EVENT_THRESHOLD",
                    "candidate_generation": None,
                    "final_decision": final_decision,
                },
            )

            save_json_atomic(state_path, state)

            append_jsonl(
                history_path,
                {
                    "step": step_number,
                    "trigger_event_id": current_event["event_id"],
                    "subtopic_count": len(state["subtopics"]),
                    "current_event_memberships": event_memberships(
                        state,
                        current_event["event_id"],
                    ),
                    "state": state,
                },
            )

            print(
                "  -> 자동 처리 | "
                f"현재 Event {len(available_event_ids)}개 < "
                f"임계치 {args.min_events_for_subtopic}"
            )

            if args.sleep > 0:
                time.sleep(args.sleep)

            continue

        candidate_payload = build_candidate_payload(
            topic_name=args.topic_name,
            current_event=current_event,
            past_events=past_events,
            state=state,
        )

        try:
            print("  -> 1단계: Subtopic 후보 생성")

            candidate_call = run_json_call_with_retries(
                client=client,
                model=model,
                prompt=candidate_prompt,
                payload=candidate_payload,
                temperature=args.candidate_temperature,
                max_tokens=args.candidate_max_tokens,
                json_mode=args.json_mode,
                max_retries=args.max_retries,
                retry_wait=args.retry_wait,
                validate_fn=lambda raw: validate_candidate_output(
                    raw,
                    current_event_id=current_event["event_id"],
                    available_event_ids=available_event_ids,
                    state=state,
                ),
            )

            candidate_decision = candidate_call["result"]

            sanitization_notes = candidate_decision.get(
                "sanitization_notes",
                [],
            )

            if sanitization_notes:
                print(
                    f"  -> Candidate 자동 정리 "
                    f"{len(sanitization_notes)}건 "
                    f"(재시도 대신 DROP/필터)"
                )

            existing_pair_count = sum(
                len(item["candidate_event_ids"])
                for item in candidate_decision[
                    "existing_membership_candidates"
                ]
            )
            new_pair_count = sum(
                len(item["candidate_event_ids"])
                for item in candidate_decision[
                    "new_subtopic_candidates"
                ]
            )

            print(
                "  -> 후보 완료 | "
                f"기존 귀속 검증쌍 {existing_pair_count}개 | "
                f"신규 후보 {len(candidate_decision['new_subtopic_candidates'])}개 "
                f"({new_pair_count} Event 검증)"
            )

            print("  -> 2단계: 2-phase 병렬 Membership Validation")

            final_decision = build_final_decision(
                client=client,
                model=model,
                validator_prompt=validator_prompt,
                topic_name=args.topic_name,
                candidate_decision=candidate_decision,
                state=state,
                event_by_id=event_by_id,
                validator_temperature=args.validator_temperature,
                validator_max_tokens=args.validator_max_tokens,
                json_mode=args.json_mode,
                max_retries=args.max_retries,
                retry_wait=args.retry_wait,
                validator_workers=args.validator_workers,
                min_new_keep=args.min_new_keep,
                step_number=step_number,
                current_event_id=current_event["event_id"],
                membership_log_path=membership_log_path,
            )

            state = apply_final_decision(
                state=state,
                final_decision=final_decision,
                current_event=current_event,
                step_number=step_number,
            )

            memberships = event_memberships(
                state,
                current_event["event_id"],
            )

            append_jsonl(
                decisions_path,
                {
                    "step": step_number,
                    "topic_name": args.topic_name,
                    "current_event_id": current_event["event_id"],
                    "candidate_generation": {
                        "decision": candidate_decision,
                        "model": model,
                        "attempt": candidate_call["attempt"],
                        "elapsed_sec": candidate_call["elapsed_sec"],
                        "usage": candidate_call["usage"],
                    },
                    "final_decision": final_decision,
                },
            )

            save_json_atomic(state_path, state)

            append_jsonl(
                history_path,
                {
                    "step": step_number,
                    "trigger_event_id": current_event["event_id"],
                    "subtopic_count": len(state["subtopics"]),
                    "current_event_memberships": memberships,
                    "state": state,
                },
            )

            print(
                "  -> 완료 | "
                f"validator {final_decision['validator_call_count']}회 | "
                f"refiner {final_decision['refiner_call_count']}회 | "
                f"기존 추가 {len(final_decision['existing_subtopic_updates'])}건 | "
                f"신규 확정 {len(final_decision['new_subtopics'])}개 | "
                f"신규 탈락 {len(final_decision['dropped_new_candidates'])}개 | "
                f"현재 Event 귀속 {memberships if memberships else '없음'}"
            )

            if final_decision["new_subtopic_overlap_report"]:
                print(
                    "  -> 참고: 확정 신규 Subtopic 간 Event 겹침 "
                    f"{len(final_decision['new_subtopic_overlap_report'])}쌍"
                )

        except Exception as e:
            append_jsonl(
                failures_path,
                {
                    "step": step_number,
                    "current_event_id": current_event["event_id"],
                    "event_name": current_event["event_name"],
                    "error_type": type(e).__name__,
                    "error": str(e),
                    "fallback": (
                        "STOP"
                        if args.stop_on_event_error
                        else "PROCESSED_ONLY_NO_IMMEDIATE_MEMBERSHIP"
                    ),
                },
            )

            if args.stop_on_event_error:
                print(
                    "\n[중단] 현재 Event 처리 실패 "
                    "(--stop-on-event-error).",
                    file=sys.stderr,
                )
                raise

            # Best-effort streaming:
            # 현재 Event를 버리지는 않는다.
            # 즉시 귀속/신규 생성만 포기하고 processed Event로 남겨,
            # 이후 신규 Subtopic 후보가 이 과거 Event를 다시 참고할 수 있게 한다.
            fallback_decision = {
                "current_event_id": (
                    current_event["event_id"]
                ),
                "existing_subtopic_updates": [],
                "new_subtopics": [],
                "dropped_new_candidates": [],
                "new_subtopic_overlap_report": [],
                "validator_call_count": 0,
                "refiner_call_count": 0,
            }

            state = apply_final_decision(
                state=state,
                final_decision=fallback_decision,
                current_event=current_event,
                step_number=step_number,
            )

            append_jsonl(
                decisions_path,
                {
                    "step": step_number,
                    "topic_name": args.topic_name,
                    "current_event_id": (
                        current_event["event_id"]
                    ),
                    "auto_rule": (
                        "EVENT_PROCESSING_ERROR_FALLBACK"
                    ),
                    "error_type": type(e).__name__,
                    "error": str(e),
                    "candidate_generation": None,
                    "final_decision": (
                        fallback_decision
                    ),
                },
            )

            save_json_atomic(
                state_path,
                state,
            )

            append_jsonl(
                history_path,
                {
                    "step": step_number,
                    "trigger_event_id": (
                        current_event["event_id"]
                    ),
                    "subtopic_count": len(
                        state["subtopics"]
                    ),
                    "current_event_memberships": [],
                    "fallback": (
                        "EVENT_PROCESSING_ERROR"
                    ),
                    "state": state,
                },
            )

            print(
                "  -> [경고] Event 처리 실패. "
                "즉시 귀속은 건너뛰고 processed-only로 기록 후 계속: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
            )

        consolidation_trigger = None

        if not args.disable_consolidation:
            consolidation_trigger = consolidation_due_reason(
                state=state,
                min_events_for_subtopic=args.min_events_for_subtopic,
                every=args.consolidation_every,
                min_gap=args.consolidation_min_gap,
                new_subtopics_trigger=(
                    args.consolidation_new_subtopics_trigger
                ),
            )

        if consolidation_trigger:
            try:
                state, _ = execute_consolidation_checkpoint(
                    client=client,
                    model=model,
                    planner_prompt=consolidation_planner_prompt,
                    duplicate_validator_prompt=duplicate_validator_prompt,
                    audit_prompt=consolidation_audit_prompt,
                    label_prompt=consolidation_label_prompt,
                    topic_name=args.topic_name,
                    state=state,
                    event_by_id=event_by_id,
                    checkpoint_step=state["last_processed_step"],
                    trigger_reason=consolidation_trigger,
                    planner_temperature=(
                        args.consolidation_planner_temperature
                    ),
                    duplicate_validator_temperature=(
                        args.duplicate_validator_temperature
                    ),
                    audit_temperature=(
                        args.consolidation_audit_temperature
                    ),
                    label_temperature=(
                        args.consolidation_label_temperature
                    ),
                    planner_max_tokens=(
                        args.consolidation_planner_max_tokens
                    ),
                    duplicate_validator_max_tokens=(
                        args.duplicate_validator_max_tokens
                    ),
                    audit_max_tokens=(
                        args.consolidation_audit_max_tokens
                    ),
                    label_max_tokens=(
                        args.consolidation_label_max_tokens
                    ),
                    json_mode=args.json_mode,
                    max_retries=args.consolidation_max_retries,
                    retry_wait=args.retry_wait,
                    audit_batch_size=(
                        args.consolidation_audit_batch_size
                    ),
                    workers=args.consolidation_workers,
                    state_path=state_path,
                    history_path=history_path,
                    consolidation_log_path=consolidation_log_path,
                    audit_log_path=consolidation_audit_log_path,
                    duplicate_log_path=duplicate_validation_log_path,
                )
    
            except Exception as e:
                append_jsonl(
                    failures_path,
                    {
                        "step": state.get(
                            "last_processed_step",
                            0,
                        ),
                        "current_event_id": (
                            state.get(
                                "last_processed_event_id"
                            )
                        ),
                        "error_scope": (
                            "CONSOLIDATION"
                        ),
                        "trigger_reason": (
                            consolidation_trigger
                        ),
                        "error_type": (
                            type(e).__name__
                        ),
                        "error": str(e),
                        "fallback": (
                            "STOP"
                            if args.stop_on_consolidation_error
                            else "KEEP_CANONICAL_STATE_AND_POSTPONE"
                        ),
                    },
                )

                if args.stop_on_consolidation_error:
                    raise

                # 같은 실패를 다음 Event마다 반복하지 않도록
                # 현재 step을 checkpoint 시도로 기록하고 state는 그대로 유지한다.
                state[
                    "last_consolidated_step"
                ] = state.get(
                    "last_processed_step",
                    0,
                )
                state[
                    "consolidation_count"
                ] = (
                    int(
                        state.get(
                            "consolidation_count",
                            0,
                        )
                    )
                    + 1
                )

                save_json_atomic(
                    state_path,
                    state,
                )

                append_jsonl(
                    consolidation_log_path,
                    {
                        "checkpoint_step": (
                            state.get(
                                "last_processed_step",
                                0,
                            )
                        ),
                        "trigger_reason": (
                            consolidation_trigger
                        ),
                        "status": (
                            "EXCEPTION_FALLBACK_NO_CHANGE"
                        ),
                        "error_type": (
                            type(e).__name__
                        ),
                        "error": str(e),
                        "model": model,
                        "consolidation_version": (
                            CONSOLIDATION_VERSION
                        ),
                    },
                )

                print(
                    "  -> [경고] Consolidation 실패. "
                    "canonical state 유지 후 계속: "
                    f"{type(e).__name__}: {e}",
                    file=sys.stderr,
                )

        if args.sleep > 0:
            time.sleep(args.sleep)

    print()
    print("=" * 78)
    print("이번 실행 완료")
    print(f"- 처리 Event 누계       : {len(state['processed_event_ids'])}")
    print(f"- Subtopic 수           : {len(state['subtopics'])}")
    print(f"- state                 : {state_path}")
    print(f"- decisions             : {decisions_path}")
    print(f"- membership validations: {membership_log_path}")
    print(f"- history               : {history_path}")
    print(
        f"- 마지막 Consolidation : "
        f"{state.get('last_consolidated_step', 0)}"
    )
    print(
        f"- Consolidation 횟수    : "
        f"{state.get('consolidation_count', 0)}"
    )
    print(f"- consolidation log     : {consolidation_log_path}")
    print(
        f"- consolidation audits  : "
        f"{consolidation_audit_log_path}"
    )
    print(
        f"- duplicate validations : "
        f"{duplicate_validation_log_path}"
    )
    print("=" * 78)


if __name__ == "__main__":
    main()
