import argparse
import copy
import json
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

DEFAULT_MODEL = "@preset/hannoon-subtopic"
ALLOWED_SUBTOPIC_TYPES = {"FOCAL_POINT", "PROCESS", "RECURRING_ISSUE"}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path} {line_no}번째 줄 JSON 파싱 실패: {e}") from e
            if not isinstance(obj, dict):
                raise ValueError(f"{path} {line_no}번째 줄이 JSON object가 아닙니다.")
            rows.append(obj)
    return rows


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def save_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


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


def split_summary_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+", text)
    parts = [part.strip() for part in parts if part.strip()]
    return parts or [text]


def build_summary_sentence_items(event_profile: dict) -> list[dict]:
    return [
        {"index": i, "text": s}
        for i, s in enumerate(split_summary_sentences(event_profile.get("event_summary") or ""))
    ]


def strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, count=1, flags=re.I)
        text = re.sub(r"\s*```$", "", text, count=1)
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
        obj = json.loads(cleaned[start : end + 1])
    if not isinstance(obj, dict):
        raise ValueError("응답 JSON 최상위가 객체가 아닙니다.")
    return obj


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


def usage_dict(response) -> dict:
    usage = getattr(response, "usage", None)
    if not usage:
        return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
    return {
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "total_tokens": getattr(usage, "total_tokens", None),
    }


def call_model(
    *, client: OpenAI, model: str, prompt: str, payload: str,
    temperature: float, max_tokens: int, json_mode: bool,
    validation_feedback: str | None = None,
    previous_bad_output: str | None = None,
):
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": payload},
    ]
    if validation_feedback:
        if previous_bad_output:
            messages.append({"role": "assistant", "content": previous_bad_output[-6000:]})
        messages.append({
            "role": "user",
            "content": (
                "직전 응답이 JSON 문법 또는 검증 규칙을 위반했습니다. "
                "입력 데이터는 유지한 채 오류를 수정하여 유효한 JSON 객체 하나만 반환하세요.\n\n"
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


def run_json_call_with_retries(
    *, client: OpenAI, model: str, prompt: str, payload: str,
    temperature: float, max_tokens: int, json_mode: bool,
    max_retries: int, retry_wait: float, validate_fn,
) -> dict:
    last_error = None
    feedback = None
    previous_bad_output = None
    for attempt in range(1, max_retries + 1):
        started = time.time()
        content = None
        try:
            response = call_model(
                client=client, model=model, prompt=prompt, payload=payload,
                temperature=temperature, max_tokens=max_tokens, json_mode=json_mode,
                validation_feedback=feedback, previous_bad_output=previous_bad_output,
            )
            content = extract_text_content(response.choices[0].message.content)
            raw = parse_json_response(content)
            result = validate_fn(raw)
            return {
                "result": result,
                "attempt": attempt,
                "elapsed_sec": round(time.time() - started, 3),
                "usage": usage_dict(response),
            }
        except Exception as e:
            last_error = e
            feedback = str(e)
            if content:
                previous_bad_output = content
            if attempt < max_retries:
                wait = retry_wait * attempt
                print(
                    f"    [재시도 {attempt}/{max_retries}] {type(e).__name__}: {e} -> {wait:.1f}초",
                    file=sys.stderr,
                )
                time.sleep(wait)
    raise last_error


def validate_event_local_membership_criterion(criterion: str, *, label: str) -> str:
    criterion = re.sub(r"\s+", " ", criterion.strip())
    criterion_cf = criterion.casefold()
    forbidden = [
        "여러 이벤트", "여러 event", "복수 이벤트", "둘 이상의 이벤트",
        "이벤트들에서", "이벤트들에", "여러 사건", "공통적으로 제시",
        "공통적으로 등장", "반복적으로 제시", "반복적으로 등장",
        "전체 흐름에서 반복",
    ]
    for phrase in forbidden:
        if phrase.casefold() in criterion_cf:
            raise ValueError(
                f"{label}.membership_criterion은 Event 하나만 보고 판정할 수 있어야 합니다. 금지 표현: {phrase}"
            )
    if "이 event" not in criterion_cf and "이 이벤트" not in criterion_cf:
        raise ValueError(
            f"{label}.membership_criterion은 반드시 '이 Event' 또는 '이 이벤트'를 주어로 사용해야 합니다."
        )
    return criterion


def repair_name(name: Any, common_basis: str) -> tuple[str, str]:
    if isinstance(name, str):
        value = re.sub(r"\s+", " ", name.strip())
        if value and len(value.split()) <= 6:
            return value, "OK"
    basis = re.sub(r"\s+", " ", (common_basis or "").strip())
    first = re.split(r"[.!?]", basis, maxsplit=1)[0].strip()
    words = first.split()
    candidate = " ".join(words[:6]).strip(" ,/&+|;:.!?")
    return (candidate or "세부 관점"), "FALLBACK"


def build_discovery_payload(*, topic_name: str, profiles: list[dict], state: dict, max_candidates: int) -> str:
    events = [
        {
            "event_id": e["event_id"],
            "event_name": e["event_name"],
            "representative_published_date": e["representative_published_date"],
        }
        for e in sorted(profiles, key=event_sort_key)
    ]
    existing_subtopics = [
        {
            "subtopic_id": st["subtopic_id"],
            "name": st["name"],
            "type": st["type"],
            "common_basis": st["common_basis"],
            "membership_criterion": st["membership_criterion"],
            "event_ids": list(st.get("event_ids", [])),
        }
        for st in state.get("subtopics", [])
    ]
    return json.dumps(
        {
            "topic_name": topic_name,
            "events": events,
            "existing_subtopics": existing_subtopics,
            "max_candidates": max_candidates,
        },
        ensure_ascii=False,
        indent=2,
    )


def validate_discovery_output(raw: dict, *, profile_ids: set[str], state: dict, max_candidates: int) -> dict:
    candidates = raw.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("candidates가 배열이 아닙니다.")

    existing_basis = {
        normalize_text(st.get("common_basis")) for st in state.get("subtopics", [])
        if normalize_text(st.get("common_basis"))
    }
    existing_criteria = {
        normalize_text(st.get("membership_criterion")) for st in state.get("subtopics", [])
        if normalize_text(st.get("membership_criterion"))
    }

    normalized = []
    notes = []
    seen_basis = set()
    seen_criterion = set()

    for idx, item in enumerate(candidates):
        if len(normalized) >= max_candidates:
            notes.append(f"candidate[{idx}] DROP: max_candidates 초과")
            continue
        if not isinstance(item, dict):
            notes.append(f"candidate[{idx}] DROP: object 아님")
            continue

        stype = item.get("type")
        common_basis = item.get("common_basis")
        criterion = item.get("membership_criterion")
        reason = item.get("reason")
        ids = item.get("candidate_event_ids")

        if stype not in ALLOWED_SUBTOPIC_TYPES:
            notes.append(f"candidate[{idx}] DROP: 잘못된 type")
            continue
        if not isinstance(common_basis, str) or not common_basis.strip():
            notes.append(f"candidate[{idx}] DROP: common_basis 비어 있음")
            continue
        if not isinstance(criterion, str) or not criterion.strip():
            notes.append(f"candidate[{idx}] DROP: membership_criterion 비어 있음")
            continue
        if not isinstance(reason, str) or not reason.strip():
            notes.append(f"candidate[{idx}] DROP: reason 비어 있음")
            continue
        if not isinstance(ids, list):
            notes.append(f"candidate[{idx}] DROP: candidate_event_ids 배열 아님")
            continue

        clean_ids = []
        seen_ids = set()
        for event_id in ids:
            if isinstance(event_id, str) and event_id in profile_ids and event_id not in seen_ids:
                seen_ids.add(event_id)
                clean_ids.append(event_id)
        if len(clean_ids) < 2:
            notes.append(f"candidate[{idx}] DROP: 유효 Event ID 2개 미만")
            continue

        common_basis = re.sub(r"\s+", " ", common_basis.strip())
        try:
            criterion = validate_event_local_membership_criterion(criterion, label=f"candidate[{idx}]")
        except Exception as e:
            notes.append(f"candidate[{idx}] DROP: {e}")
            continue

        basis_key = normalize_text(common_basis)
        criterion_key = normalize_text(criterion)
        if basis_key in existing_basis or criterion_key in existing_criteria:
            notes.append(f"candidate[{idx}] DROP: 기존 Subtopic과 동일 기준")
            continue
        if basis_key in seen_basis or criterion_key in seen_criterion:
            notes.append(f"candidate[{idx}] DROP: 같은 discovery 호출 내 중복")
            continue

        name, name_status = repair_name(item.get("name"), common_basis)
        normalized.append({
            "name": name,
            "display_name_status": name_status,
            "type": stype,
            "common_basis": common_basis,
            "membership_criterion": criterion,
            "candidate_event_ids": clean_ids,
            "reason": reason.strip(),
        })
        seen_basis.add(basis_key)
        seen_criterion.add(criterion_key)

    return {"candidates": normalized, "sanitization_notes": notes}


def build_validator_payload(*, topic_name: str, subtopic: dict, event_profile: dict) -> str:
    ep = dict(event_profile)
    ep["summary_sentences"] = build_summary_sentence_items(event_profile)
    ep["direct_evidence_text"] = event_profile["event_name"]
    payload = {
        "topic_name": topic_name,
        "subtopic": {
            "name": subtopic["name"],
            "type": subtopic["type"],
            "common_basis": subtopic["common_basis"],
            "membership_criterion": subtopic["membership_criterion"],
        },
        "event_profile": ep,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def validate_membership_output(raw: dict, *, event_profile: dict) -> dict:
    event_id = event_profile["event_id"]
    if raw.get("event_id") != event_id:
        raise ValueError("validator.event_id가 입력 Event와 다릅니다.")

    relation = raw.get("relation")
    if relation not in {"DIRECT", "RELATED", "INDIRECT", "NONE"}:
        raise ValueError("validator.relation 값 오류")

    related_path = raw.get("related_path")
    allowed_related_paths = {"SAME_CONCRETE_MECHANISM", "EXPLICIT_DIRECT_RELATION"}
    if relation == "RELATED":
        if related_path not in allowed_related_paths:
            raise ValueError("RELATED의 related_path 값 오류")
    elif related_path is not None:
        raise ValueError(f"relation={relation}이면 related_path는 null이어야 합니다.")

    expected_scope = {
        "DIRECT": "PRIMARY",
        "RELATED": "DIRECT_CONTEXT",
        "INDIRECT": "BACKGROUND_ONLY",
        "NONE": None,
    }[relation]
    evidence_scope = raw.get("evidence_scope")
    if evidence_scope != expected_scope:
        raise ValueError(f"relation={relation}이면 evidence_scope={expected_scope!r}이어야 합니다.")

    source = raw.get("source")
    anchor_name = raw.get("anchor_name")
    sentence_index = raw.get("summary_sentence_index")
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("validator.reason이 비어 있습니다.")

    anchors = [
        a.get("name") for a in event_profile.get("anchors", [])
        if isinstance(a, dict) and isinstance(a.get("name"), str) and a.get("name").strip()
    ]
    anchor_map = {normalize_text(x): x for x in anchors}
    summary_sentences = split_summary_sentences(event_profile.get("event_summary") or "")

    if relation == "NONE":
        if source is not None or anchor_name is not None or sentence_index is not None:
            raise ValueError("NONE이면 source/anchor/index는 모두 null이어야 합니다.")
        return {
            "event_id": event_id, "relation": "NONE", "related_path": None,
            "evidence_scope": None, "decision": "REJECT", "source": None,
            "anchor_name": None, "summary_sentence_index": None,
            "evidence_text": None, "reason": reason.strip(),
        }

    if relation == "DIRECT":
        # 17_71 핵심 안전장치: DIRECT의 근거는 EVENT_NAME만 허용.
        if source != "EVENT_NAME":
            raise ValueError("DIRECT이면 source는 반드시 EVENT_NAME이어야 합니다.")
        if anchor_name is not None or sentence_index is not None:
            raise ValueError("DIRECT이면 anchor_name/index는 null이어야 합니다.")
        normalized_anchor = None
        normalized_sentence_index = None
        evidence_text = event_profile["event_name"]
    else:
        if source not in {"ANCHOR", "EVENT_SUMMARY"}:
            raise ValueError(f"relation={relation}이면 source는 ANCHOR/EVENT_SUMMARY여야 합니다.")
        if relation == "RELATED" and related_path == "EXPLICIT_DIRECT_RELATION" and source != "EVENT_SUMMARY":
            raise ValueError("RELATED+EXPLICIT_DIRECT_RELATION이면 EVENT_SUMMARY 근거여야 합니다.")
        if source == "ANCHOR":
            if not isinstance(anchor_name, str) or not anchor_name.strip():
                raise ValueError("ANCHOR 근거인데 anchor_name이 없습니다.")
            if sentence_index is not None:
                raise ValueError("ANCHOR 근거이면 index는 null이어야 합니다.")
            key = normalize_text(anchor_name)
            if key not in anchor_map:
                raise ValueError(f"실제 profile에 없는 anchor: {anchor_name}")
            normalized_anchor = anchor_map[key]
            normalized_sentence_index = None
            evidence_text = normalized_anchor
        else:
            if anchor_name is not None:
                raise ValueError("EVENT_SUMMARY 근거이면 anchor_name은 null이어야 합니다.")
            if not isinstance(sentence_index, int) or not (0 <= sentence_index < len(summary_sentences)):
                raise ValueError("summary_sentence_index 범위 오류")
            normalized_anchor = None
            normalized_sentence_index = sentence_index
            evidence_text = summary_sentences[sentence_index]

    return {
        "event_id": event_id,
        "relation": relation,
        "related_path": related_path,
        "evidence_scope": evidence_scope,
        "decision": "KEEP" if relation == "DIRECT" else "REJECT",
        "source": source,
        "anchor_name": normalized_anchor,
        "summary_sentence_index": normalized_sentence_index,
        "evidence_text": evidence_text,
        "reason": reason.strip(),
    }


def validate_one_membership(
    *, client: OpenAI, model: str, validator_prompt: str, topic_name: str,
    subtopic: dict, event_profile: dict, temperature: float, max_tokens: int,
    json_mode: bool, max_retries: int, retry_wait: float,
) -> dict:
    payload = build_validator_payload(topic_name=topic_name, subtopic=subtopic, event_profile=event_profile)
    return run_json_call_with_retries(
        client=client, model=model, prompt=validator_prompt, payload=payload,
        temperature=temperature, max_tokens=max_tokens, json_mode=json_mode,
        max_retries=max_retries, retry_wait=retry_wait,
        validate_fn=lambda raw: validate_membership_output(raw, event_profile=event_profile),
    )


def run_membership_parallel(
    *, candidates: list[dict], event_by_id: dict[str, dict], client: OpenAI,
    model: str, validator_prompt: str, topic_name: str, temperature: float,
    max_tokens: int, json_mode: bool, max_retries: int, retry_wait: float,
    workers: int,
) -> list[dict]:
    tasks = []
    for ci, candidate in enumerate(candidates, 1):
        subtopic = {
            "name": candidate["name"],
            "type": candidate["type"],
            "common_basis": candidate["common_basis"],
            "membership_criterion": candidate["membership_criterion"],
        }
        for event_id in candidate["candidate_event_ids"]:
            tasks.append({"candidate_index": ci, "candidate": candidate, "subtopic": subtopic, "event_id": event_id})

    def one(task: dict) -> dict:
        call = validate_one_membership(
            client=client, model=model, validator_prompt=validator_prompt,
            topic_name=topic_name, subtopic=task["subtopic"],
            event_profile=event_by_id[task["event_id"]],
            temperature=temperature, max_tokens=max_tokens,
            json_mode=json_mode, max_retries=max_retries, retry_wait=retry_wait,
        )
        return {"task": task, "call": call}

    if not tasks:
        return []
    if workers <= 1 or len(tasks) == 1:
        return [one(t) for t in tasks]

    ordered = [None] * len(tasks)
    with ThreadPoolExecutor(max_workers=min(workers, len(tasks))) as executor:
        future_to_index = {executor.submit(one, t): i for i, t in enumerate(tasks)}
        for future in as_completed(future_to_index):
            ordered[future_to_index[future]] = future.result()
    return ordered


def next_subtopic_id(state: dict) -> str:
    return f"ST{int(state.get('next_subtopic_number', 1)):03d}"


def main():
    parser = argparse.ArgumentParser(
        description="17_71 결과 위에 Missing Subtopic Discovery만 독립적으로 붙여 보는 offline recovery 실험"
    )
    parser.add_argument("--profiles", required=True)
    parser.add_argument("--baseline-state", required=True)
    parser.add_argument("--topic-name", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--discovery-prompt", default="17_77_missing_subtopic_discovery_prompt.md")
    parser.add_argument("--discovery-schema", default="17_77_missing_subtopic_discovery_schema.json")
    parser.add_argument("--validator-prompt", default="17_77_membership_validator_prompt_periodic7.md")
    parser.add_argument("--validator-schema", default="17_77_membership_validator_schema_periodic7.json")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-candidates", type=int, default=3)
    parser.add_argument("--min-keep", type=int, default=2)
    parser.add_argument("--discovery-temperature", type=float, default=0.0)
    parser.add_argument("--validator-temperature", type=float, default=0.0)
    parser.add_argument("--discovery-max-tokens", type=int, default=1800)
    parser.add_argument("--validator-max-tokens", type=int, default=800)
    parser.add_argument("--validator-workers", type=int, default=6)
    parser.add_argument("--max-retries", type=int, default=4)
    parser.add_argument("--retry-wait", type=float, default=3.0)
    parser.add_argument("--json-mode", action="store_true")
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent
    def resolve(path_str: str) -> Path:
        p = Path(path_str)
        return p if p.is_absolute() else (base_dir / p)

    profile_path = resolve(args.profiles)
    baseline_state_path = resolve(args.baseline_state)
    discovery_prompt_path = resolve(args.discovery_prompt)
    discovery_schema_path = resolve(args.discovery_schema)
    validator_prompt_path = resolve(args.validator_prompt)
    validator_schema_path = resolve(args.validator_schema)
    env_path = Path(args.env_file)
    if not env_path.is_absolute():
        # .env는 사용자가 현재 작업 폴더에 두는 경우가 많아 cwd를 먼저 사용.
        cwd_env = Path.cwd() / env_path
        env_path = cwd_env if cwd_env.exists() else resolve(args.env_file)

    for p, label in [
        (profile_path, "profiles"), (baseline_state_path, "baseline state"),
        (discovery_prompt_path, "discovery prompt"), (discovery_schema_path, "discovery schema"),
        (validator_prompt_path, "validator prompt"), (validator_schema_path, "validator schema"),
        (env_path, ".env"),
    ]:
        if not p.exists():
            parser.error(f"{label} 파일을 찾을 수 없습니다: {p}")

    # schema 파일은 문법/배포 누락 검사에 사용한다.
    load_json(discovery_schema_path)
    load_json(validator_schema_path)

    output_dir = Path(args.output_dir)
    if args.reset and output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    profiles = load_jsonl(profile_path)
    required = {
        "event_id", "event_name", "representative_published_date", "first_published_at",
        "event_summary", "anchors",
    }
    for e in profiles:
        missing = required - set(e)
        if missing:
            parser.error(f"{e.get('event_id','?')} profile 필드 누락: {', '.join(sorted(missing))}")
    profiles.sort(key=event_sort_key)
    event_by_id = {e["event_id"]: e for e in profiles}
    if len(event_by_id) != len(profiles):
        parser.error("profiles에 중복 event_id가 있습니다.")

    baseline_state = load_json(baseline_state_path)
    if baseline_state.get("topic_name") != args.topic_name:
        parser.error(
            f"baseline topic_name={baseline_state.get('topic_name')!r} 와 --topic-name={args.topic_name!r}가 다릅니다."
        )

    # baseline state가 처리한 Event만 recovery 입력으로 사용.
    processed_ids = [eid for eid in baseline_state.get("processed_event_ids", []) if eid in event_by_id]
    if not processed_ids:
        parser.error("baseline state에 유효한 processed_event_ids가 없습니다.")
    processed_profiles = [event_by_id[eid] for eid in processed_ids]
    processed_profiles.sort(key=event_sort_key)
    processed_id_set = {e["event_id"] for e in processed_profiles}

    load_dotenv(dotenv_path=env_path, override=False)
    api_key = os.getenv("QWEN_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("OPENROUTER_API_KEY")
    base_url = os.getenv("QWEN_BASE_URL") or os.getenv("OPENAI_BASE_URL") or os.getenv("OPENROUTER_BASE_URL")
    if not api_key:
        parser.error(".env에서 API key를 찾지 못했습니다.")
    if not base_url:
        parser.error(".env에서 API base URL을 찾지 못했습니다.")
    model = args.model or os.getenv("OPENROUTER_SUBTOPIC_MODEL") or os.getenv("QWEN_MODEL") or DEFAULT_MODEL
    client = OpenAI(api_key=api_key, base_url=base_url)

    discovery_prompt = load_text(discovery_prompt_path)
    validator_prompt = load_text(validator_prompt_path)

    print("=" * 78)
    print("17_73 Missing Subtopic Recovery - PERIODIC CHECKPOINT EXPERIMENT")
    print(f"- topic_name       : {args.topic_name}")
    print(f"- processed events : {len(processed_profiles)}")
    print(f"- baseline active  : {len(baseline_state.get('subtopics', []))}")
    print(f"- max candidates   : {args.max_candidates}")
    print(f"- min DIRECT KEEP  : {args.min_keep}")
    print("- discovery context: event_id + event_name + date ONLY")
    print("- membership       : 17_71 validator, DIRECT=EVENT_NAME only")
    print("- baseline state   : READ-ONLY; recovered state is written separately")
    print("=" * 78)

    payload = build_discovery_payload(
        topic_name=args.topic_name,
        profiles=processed_profiles,
        state=baseline_state,
        max_candidates=args.max_candidates,
    )

    discovery_call = run_json_call_with_retries(
        client=client, model=model, prompt=discovery_prompt, payload=payload,
        temperature=args.discovery_temperature, max_tokens=args.discovery_max_tokens,
        json_mode=args.json_mode, max_retries=args.max_retries, retry_wait=args.retry_wait,
        validate_fn=lambda raw: validate_discovery_output(
            raw, profile_ids=processed_id_set, state=baseline_state, max_candidates=args.max_candidates
        ),
    )
    candidates = discovery_call["result"]["candidates"]
    print(f"Discovery 후보: {len(candidates)}개")
    for i, c in enumerate(candidates, 1):
        print(f"  {i}. {c['name']} | {c['type']} | candidate events={len(c['candidate_event_ids'])}")

    membership_results = run_membership_parallel(
        candidates=candidates, event_by_id=event_by_id, client=client, model=model,
        validator_prompt=validator_prompt, topic_name=args.topic_name,
        temperature=args.validator_temperature, max_tokens=args.validator_max_tokens,
        json_mode=args.json_mode, max_retries=args.max_retries, retry_wait=args.retry_wait,
        workers=args.validator_workers,
    )

    membership_log_path = output_dir / "17_77_missing_discovery_membership_validations.jsonl"
    by_candidate = {i: [] for i in range(1, len(candidates) + 1)}
    for item in membership_results:
        task = item["task"]
        call = item["call"]
        val = call["result"]
        by_candidate[task["candidate_index"]].append(val)
        append_jsonl(membership_log_path, {
            "candidate_index": task["candidate_index"],
            "target_name": task["candidate"]["name"],
            "common_basis": task["candidate"]["common_basis"],
            "membership_criterion": task["candidate"]["membership_criterion"],
            "candidate_event_id": task["event_id"],
            "validation": val,
            "model": model,
            "attempt": call["attempt"],
            "elapsed_sec": call["elapsed_sec"],
            "usage": call["usage"],
        })

    recovered_state = copy.deepcopy(baseline_state)
    created = []
    dropped = []

    # deterministic exact duplicate guards are checked again after membership.
    existing_basis = {normalize_text(st.get("common_basis")) for st in recovered_state.get("subtopics", [])}
    existing_criteria = {normalize_text(st.get("membership_criterion")) for st in recovered_state.get("subtopics", [])}
    existing_event_sets = {frozenset(st.get("event_ids", [])) for st in recovered_state.get("subtopics", [])}

    for i, candidate in enumerate(candidates, 1):
        vals = by_candidate.get(i, [])
        kept_ids = [v["event_id"] for v in vals if v.get("decision") == "KEEP"]
        # original chronological order
        kept_set = set(kept_ids)
        kept_ids = [e["event_id"] for e in processed_profiles if e["event_id"] in kept_set]

        if len(kept_ids) < args.min_keep:
            dropped.append({
                "candidate_index": i,
                **candidate,
                "kept_event_ids": kept_ids,
                "drop_reason": f"17_71 Membership Validator DIRECT KEEP가 {args.min_keep}개 미만",
            })
            continue

        basis_key = normalize_text(candidate["common_basis"])
        criterion_key = normalize_text(candidate["membership_criterion"])
        if basis_key in existing_basis or criterion_key in existing_criteria:
            dropped.append({
                "candidate_index": i, **candidate, "kept_event_ids": kept_ids,
                "drop_reason": "기존 active Subtopic과 동일 common_basis/criterion",
            })
            continue
        if frozenset(kept_ids) in existing_event_sets:
            dropped.append({
                "candidate_index": i, **candidate, "kept_event_ids": kept_ids,
                "drop_reason": "기존 active Subtopic과 Event membership이 완전히 동일",
            })
            continue

        sid = next_subtopic_id(recovered_state)
        kept_validations = [v for v in vals if v.get("decision") == "KEEP"]
        new_st = {
            "subtopic_id": sid,
            "name": candidate["name"],
            "display_name_status": candidate.get("display_name_status", "OK"),
            "type": candidate["type"],
            "common_basis": candidate["common_basis"],
            "membership_criterion": candidate["membership_criterion"],
            "event_ids": kept_ids,
            "creation_membership_validations": kept_validations,
            "created_at_step": recovered_state.get("last_processed_step", len(processed_profiles)),
            "created_by_event_id": recovered_state.get("last_processed_event_id"),
            "candidate_reason": candidate["reason"],
            "creation_scope_refinement": None,
            "refinement_history": [],
            "update_history": [],
            "creation_mode": "MISSING_SUBTOPIC_RECOVERY_PERIODIC_EXPERIMENT",
        }
        recovered_state.setdefault("subtopics", []).append(new_st)
        recovered_state["next_subtopic_number"] = int(recovered_state.get("next_subtopic_number", 1)) + 1
        created.append(new_st)
        existing_basis.add(basis_key)
        existing_criteria.add(criterion_key)
        existing_event_sets.add(frozenset(kept_ids))

    state_path = output_dir / "17_77_subtopic_state_recovered.json"
    save_json(state_path, recovered_state)

    discovery_log_path = output_dir / "17_77_missing_subtopic_discovery.jsonl"
    append_jsonl(discovery_log_path, {
        "topic_name": args.topic_name,
        "baseline_active_subtopics": len(baseline_state.get("subtopics", [])),
        "processed_event_count": len(processed_profiles),
        "model": model,
        "discovery_call": {
            "result": discovery_call["result"],
            "attempt": discovery_call["attempt"],
            "elapsed_sec": discovery_call["elapsed_sec"],
            "usage": discovery_call["usage"],
        },
        "created_subtopics": [
            {
                "subtopic_id": st["subtopic_id"], "name": st["name"], "type": st["type"],
                "common_basis": st["common_basis"], "membership_criterion": st["membership_criterion"],
                "event_ids": st["event_ids"],
            }
            for st in created
        ],
        "dropped_candidates": dropped,
    })

    summary = {
        "topic_name": args.topic_name,
        "processed_event_count": len(processed_profiles),
        "baseline_active_subtopic_count": len(baseline_state.get("subtopics", [])),
        "discovery_candidate_count": len(candidates),
        "created_missing_subtopic_count": len(created),
        "recovered_active_subtopic_count": len(recovered_state.get("subtopics", [])),
        "created_subtopics": [
            {"subtopic_id": st["subtopic_id"], "name": st["name"], "event_ids": st["event_ids"]}
            for st in created
        ],
        "dropped_candidate_count": len(dropped),
    }
    save_json(output_dir / "17_77_recovery_summary.json", summary)

    print()
    print("결과")
    print(f"- baseline active : {summary['baseline_active_subtopic_count']}")
    print(f"- discovery 후보 : {summary['discovery_candidate_count']}")
    print(f"- 신규 확정      : {summary['created_missing_subtopic_count']}")
    print(f"- recovered active: {summary['recovered_active_subtopic_count']}")
    if created:
        for st in created:
            print(f"  + {st['subtopic_id']} {st['name']} | {len(st['event_ids'])} Events | {st['event_ids']}")
    print(f"- state   : {state_path}")
    print(f"- summary : {output_dir / '17_77_recovery_summary.json'}")
    print(f"- logs    : {discovery_log_path}")


if __name__ == "__main__":
    main()
