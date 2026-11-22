import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI


DEFAULT_MODEL = "@preset/hannoon"

ALLOWED_ANCHOR_TYPES = {
    "ENTITY",
    "ISSUE",
    "PROCESS",
    "POLICY_OR_SYSTEM",
}


def load_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def load_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_jsonl(path: str) -> list[dict]:
    rows = []

    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()

            if not line:
                continue

            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"{path} {line_no}번째 줄 JSON 파싱 실패: {e}"
                ) from e

    return rows


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def extract_text_content(message_content: Any) -> str:
    """
    OpenAI-compatible API 응답의 message.content가
    문자열이거나 배열 형태인 경우 모두 처리한다.
    """
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
    """
    1) 응답 전체를 JSON으로 파싱
    2) 실패하면 가장 바깥쪽 { ... } 구간을 한 번 복구 시도
    """
    cleaned = strip_code_fence(text)

    try:
        obj = json.loads(cleaned)

        if not isinstance(obj, dict):
            raise ValueError(
                "응답 JSON 최상위가 객체(object)가 아닙니다."
            )

        return obj

    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")

        if start == -1 or end == -1 or end <= start:
            raise

        candidate = cleaned[start:end + 1]
        obj = json.loads(candidate)

        if not isinstance(obj, dict):
            raise ValueError(
                "응답 JSON 최상위가 객체(object)가 아닙니다."
            )

        return obj


def normalize_and_validate_profile(
    raw: dict,
    event_input: dict,
) -> tuple[dict, list[str]]:
    """
    모델이 생성해야 할 핵심 값은 event_summary / anchors.

    event_id, event_name,
    representative_published_date, first_published_at은
    입력값이 기준이므로 모델이 다르게 반환해도 입력값으로 고정한다.
    """
    warnings = []

    fixed_fields = [
        "event_id",
        "event_name",
        "representative_published_date",
        "first_published_at",
    ]

    for field in fixed_fields:
        if raw.get(field) != event_input.get(field):
            warnings.append(
                f"{field} 모델 반환값이 입력과 달라 입력값으로 교정"
            )

    summary = raw.get("event_summary")

    if not isinstance(summary, str) or not summary.strip():
        raise ValueError(
            "event_summary가 비어 있거나 문자열이 아닙니다."
        )

    anchors_raw = raw.get("anchors")

    if not isinstance(anchors_raw, list):
        raise ValueError(
            "anchors가 배열이 아닙니다."
        )

    anchors = []
    seen = set()

    for idx, anchor in enumerate(anchors_raw, start=1):
        if not isinstance(anchor, dict):
            raise ValueError(
                f"anchors[{idx}]가 객체가 아닙니다."
            )

        name = anchor.get("name")
        anchor_type = anchor.get("type")
        evidence_role = anchor.get("evidence_role")

        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f"anchors[{idx}].name이 비어 있습니다."
            )

        if anchor_type not in ALLOWED_ANCHOR_TYPES:
            raise ValueError(
                f"anchors[{idx}].type={anchor_type!r} "
                "은 허용되지 않습니다."
            )

        if (
            not isinstance(evidence_role, str)
            or not evidence_role.strip()
        ):
            raise ValueError(
                f"anchors[{idx}].evidence_role이 비어 있습니다."
            )

        key = (
            name.strip(),
            anchor_type,
        )

        if key in seen:
            warnings.append(
                f"중복 anchor 제거: "
                f"{name.strip()} / {anchor_type}"
            )
            continue

        seen.add(key)

        anchors.append({
            "name": name.strip(),
            "type": anchor_type,
            "evidence_role": evidence_role.strip(),
        })

    profile = {
        "event_id": event_input["event_id"],
        "event_name": event_input["event_name"],
        "representative_published_date":
            event_input["representative_published_date"],
        "first_published_at":
            event_input["first_published_at"],
        "event_summary":
            summary.strip(),
        "anchors":
            anchors,
    }

    return profile, warnings


def existing_event_ids(output_path: Path) -> set[str]:
    if not output_path.exists():
        return set()

    result = set()

    with open(output_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()

            if not line:
                continue

            try:
                row = json.loads(line)
                event_id = row.get("event_id")

                if event_id:
                    result.add(event_id)

            except json.JSONDecodeError:
                continue

    return result


def build_user_payload(event_input: dict) -> str:
    """
    Event에 귀속된 articles 전체를 그대로 전달한다.
    본문 truncate / sample / 기사 수 제한을 하지 않는다.
    """
    return json.dumps(
        event_input,
        ensure_ascii=False,
        indent=2,
    )


def call_qwen(
    client: OpenAI,
    model: str,
    system_prompt: str,
    event_input: dict,
    temperature: float,
    max_tokens: int,
    json_mode: bool,
):
    kwargs = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": build_user_payload(event_input),
            },
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    if json_mode:
        kwargs["response_format"] = {
            "type": "json_object"
        }

    return client.chat.completions.create(**kwargs)


def process_one_event(
    *,
    client: OpenAI,
    model: str,
    system_prompt: str,
    event_input: dict,
    temperature: float,
    max_tokens: int,
    json_mode: bool,
    max_retries: int,
    retry_wait: float,
) -> tuple[dict, dict]:

    last_error = None

    for attempt in range(1, max_retries + 1):
        started = time.time()

        try:
            response = call_qwen(
                client=client,
                model=model,
                system_prompt=system_prompt,
                event_input=event_input,
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=json_mode,
            )

            content = extract_text_content(
                response.choices[0].message.content
            )

            raw = parse_json_response(content)

            profile, warnings = normalize_and_validate_profile(
                raw,
                event_input,
            )

            usage = getattr(response, "usage", None)

            meta = {
                "event_id":
                    event_input["event_id"],
                "model":
                    model,
                "attempt":
                    attempt,
                "elapsed_sec":
                    round(time.time() - started, 3),
                "article_count":
                    event_input.get("article_count"),
                "warnings":
                    warnings,
                "usage": {
                    "prompt_tokens":
                        getattr(
                            usage,
                            "prompt_tokens",
                            None,
                        )
                        if usage else None,
                    "completion_tokens":
                        getattr(
                            usage,
                            "completion_tokens",
                            None,
                        )
                        if usage else None,
                    "total_tokens":
                        getattr(
                            usage,
                            "total_tokens",
                            None,
                        )
                        if usage else None,
                },
            }

            return profile, meta

        except Exception as e:
            last_error = e

            if attempt < max_retries:
                wait = retry_wait * attempt

                print(
                    f"  [재시도 {attempt}/{max_retries}] "
                    f"{type(e).__name__}: {e} "
                    f"-> {wait:.1f}초 대기",
                    file=sys.stderr,
                )

                time.sleep(wait)

    raise RuntimeError(
        f"{max_retries}회 시도 후 실패: {last_error}"
    ) from last_error


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Event Profile 입력 JSONL을 Qwen API에 보내 "
            "event_summary + anchors를 생성한다."
        )
    )

    parser.add_argument(
        "--input",
        required=True,
        help="*_event_profile_input.jsonl 경로",
    )

    parser.add_argument(
        "--output",
        required=True,
        help="완성 Event Profile JSONL 출력 경로",
    )

    parser.add_argument(
        "--prompt",
        default="event_profile_extraction_prompt.md",
        help="Event Profile 추출 프롬프트 경로",
    )

    parser.add_argument(
        "--schema",
        default="event_profile_schema.json",
        help="Event Profile JSON schema 경로",
    )

    parser.add_argument(
        "--env-file",
        default=".env",
        help="API key와 URL이 저장된 env 파일. 기본 .env",
    )

    parser.add_argument(
        "--model",
        default=None,
        help=(
            "기본값은 @preset/hannoon. "
            "필요할 때만 다른 model/preset으로 override"
        ),
    )

    parser.add_argument(
        "--temperature",
        type=float,
        default=0.2,
        help="기본 0.2",
    )

    parser.add_argument(
        "--max-tokens",
        type=int,
        default=2500,
        help="Event Profile 한 건 최대 출력 토큰. 기본 2500",
    )

    parser.add_argument(
        "--json-mode",
        action="store_true",
        help=(
            "API가 response_format json_object를 "
            "지원할 때 사용"
        ),
    )

    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Event당 최대 API 시도 횟수. 기본 3",
    )

    parser.add_argument(
        "--retry-wait",
        type=float,
        default=3.0,
        help="재시도 기본 대기 초",
    )

    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="Event 처리 후 다음 요청까지 대기 초",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="처음 N개 Event만 테스트",
    )

    parser.add_argument(
        "--event-id",
        action="append",
        default=[],
        help=(
            "특정 Event만 실행. "
            "--event-id E001 --event-id E002"
        ),
    )

    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="기존 output에 있는 Event도 다시 실행",
    )

    args = parser.parse_args()

    # --------------------------------------------------
    # .env 로드
    # --------------------------------------------------
    env_path = Path(args.env_file)

    if not env_path.exists():
        parser.error(
            f".env 파일을 찾을 수 없습니다: {env_path}"
        )

    load_dotenv(
        dotenv_path=env_path,
        override=False,
    )

    # --------------------------------------------------
    # .env에서 API 정보 읽기
    #
    # 우선 QWEN_* 이름을 사용하고,
    # 기존 프로젝트의 흔한 이름도 fallback으로 지원.
    # --------------------------------------------------
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
        parser.error(
            ".env에서 API key를 찾지 못했습니다.\n"
            "다음 중 하나를 사용하세요:\n"
            "- QWEN_API_KEY\n"
            "- OPENAI_API_KEY\n"
            "- OPENROUTER_API_KEY"
        )

    if not base_url:
        parser.error(
            ".env에서 API base URL을 찾지 못했습니다.\n"
            "다음 중 하나를 사용하세요:\n"
            "- QWEN_BASE_URL\n"
            "- OPENAI_BASE_URL\n"
            "- OPENROUTER_BASE_URL"
        )

    # Event Profile용 모델 선택:
    # CLI --model > OPENROUTER_MODEL > QWEN_MODEL > 기본 preset.
    model = (
        args.model
        or os.getenv("OPENROUTER_MODEL")
        or os.getenv("QWEN_MODEL")
        or DEFAULT_MODEL
    )

    input_path = Path(args.input)
    output_path = Path(args.output)
    prompt_path = Path(args.prompt)
    schema_path = Path(args.schema)

    if not input_path.exists():
        parser.error(
            f"입력 파일을 찾을 수 없습니다: {input_path}"
        )

    if not prompt_path.exists():
        parser.error(
            f"프롬프트 파일을 찾을 수 없습니다: {prompt_path}"
        )

    if not schema_path.exists():
        parser.error(
            f"스키마 파일을 찾을 수 없습니다: {schema_path}"
        )

    load_json(str(schema_path))

    system_prompt = load_text(str(prompt_path))
    rows = load_jsonl(str(input_path))

    if args.event_id:
        wanted = set(args.event_id)

        rows = [
            row
            for row in rows
            if row.get("event_id") in wanted
        ]

        found = {
            row.get("event_id")
            for row in rows
        }

        missing = wanted - found

        if missing:
            print(
                "[주의] 입력에서 찾지 못한 event_id: "
                + ", ".join(sorted(missing)),
                file=sys.stderr,
            )

    if args.limit is not None:
        rows = rows[:args.limit]

    done = set()

    if not args.no_resume:
        done = existing_event_ids(output_path)

    rows_to_run = [
        row
        for row in rows
        if row.get("event_id") not in done
    ]

    meta_path = output_path.with_suffix(
        output_path.suffix + ".meta.jsonl"
    )

    failure_path = output_path.with_suffix(
        output_path.suffix + ".failures.jsonl"
    )

    print("=" * 72)
    print("Event Profile 생성")
    print(f"- env            : {env_path}")
    print(f"- input          : {input_path}")
    print(f"- output         : {output_path}")
    print(f"- prompt         : {prompt_path}")
    print(f"- base_url       : {base_url}")
    print(f"- model          : {model}")
    print(f"- 전체 선택 Event : {len(rows)}")
    print(
        f"- 이미 완료      : "
        f"{len(rows) - len(rows_to_run)}"
    )
    print(f"- 이번 실행      : {len(rows_to_run)}")
    print(f"- temperature    : {args.temperature}")
    print(f"- max_tokens     : {args.max_tokens}")
    print(f"- json_mode      : {args.json_mode}")
    print("=" * 72)

    if not rows_to_run:
        print("실행할 Event가 없습니다.")
        return

    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
    )

    success_count = 0
    failure_count = 0

    for index, event_input in enumerate(
        rows_to_run,
        start=1,
    ):
        event_id = event_input.get(
            "event_id",
            "?",
        )

        event_name = event_input.get(
            "event_name",
            "",
        )

        article_count = event_input.get(
            "article_count",
        )

        print(
            f"[{index}/{len(rows_to_run)}] "
            f"{event_id} | 기사 {article_count}건 | "
            f"{event_name}"
        )

        try:
            profile, meta = process_one_event(
                client=client,
                model=model,
                system_prompt=system_prompt,
                event_input=event_input,
                temperature=args.temperature,
                max_tokens=args.max_tokens,
                json_mode=args.json_mode,
                max_retries=args.max_retries,
                retry_wait=args.retry_wait,
            )

            append_jsonl(
                output_path,
                profile,
            )

            append_jsonl(
                meta_path,
                meta,
            )

            success_count += 1

            print(
                f"  -> 완료 | "
                f"anchor {len(profile['anchors'])}개"
            )

            if meta["warnings"]:
                for warning in meta["warnings"]:
                    print(
                        f"     [warning] {warning}"
                    )

        except Exception as e:
            failure_count += 1

            failure = {
                "event_id":
                    event_id,
                "event_name":
                    event_name,
                "error_type":
                    type(e).__name__,
                "error":
                    str(e),
            }

            append_jsonl(
                failure_path,
                failure,
            )

            print(
                f"  -> 실패: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
            )

        if args.sleep > 0:
            time.sleep(args.sleep)

    print()
    print("=" * 72)
    print("실행 종료")
    print(f"- 성공: {success_count}")
    print(f"- 실패: {failure_count}")
    print(f"- profile: {output_path}")
    print(f"- meta   : {meta_path}")

    if failure_count:
        print(f"- failure: {failure_path}")

    print("=" * 72)


if __name__ == "__main__":
    main()
