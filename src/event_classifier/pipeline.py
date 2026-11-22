from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys

from collector.storage import ensure_db
from db import events
from diagnostics import log_candidates
from embedding import embed_query, fit_sentences_to_max_tokens, to_vector_literal
from event_classifier import jev
from event_summary import generate_event_title
from event_classifier.prompts import (
    build_structure_article_prompt,
    build_structure_tense_retry_prompt,
)
from event_classifier.settings import (
    BATCH_SIZE,
    CANDIDATE_POOL_SIZE,
    LLM_MODEL,
)
from openai_client.client import LLMClient, load_decision_score, parse_json_object
from summary_utils import normalize_summary


_client: LLMClient | None = None


def _get_client(model: str = LLM_MODEL) -> LLMClient:
    """같은 실행 안에서는 모델별 LLM 클라이언트를 재사용한다."""
    global _client
    if _client is None or _client.model != model:
        _client = LLMClient(model=model)
    return _client


def _parse_json(response: str) -> dict:
    """LLM 응답을 JSON 객체로 정규화한다."""
    return parse_json_object(response.strip())


def _get_first_sentences(text: str, limit: int = 4) -> str:
    """이벤트 추출과 임베딩에 사용할 기사 앞부분을 잘라낸다."""
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?。！？])\s+", " ".join(text.split()))
    selected = [sentence.strip() for sentence in sentences if sentence.strip()][:limit]
    return " ".join(selected)


def _build_representative_source(
    title: str,
    ai_summary: str,
    article_excerpt: str,
) -> str:
    """대표사건 추출·검색 임베딩 공통 입력.

    기사 전체 요약(ai_summary)을 우선 쓰고(없으면 첫 문단 폴백) 제목과 자연문으로 잇는다.
    key:value 라벨을 쓰지 않는다 — ko-sRoBERTa(문장 STS 모델)에 자연문이 적합하고,
    첫 4문장 리드 앵글 차이로 대표사건이 흔들리던 문제를 요약 기반으로 안정화한다.
    """
    title_text = " ".join((title or "").split())
    body_text = " ".join((ai_summary or "").split()) or " ".join((article_excerpt or "").split())
    parts = [part for part in (title_text, body_text) if part]
    return ". ".join(parts)


_FULL_DATE_RE = re.compile(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일")
_SCHEDULE_WORDS = ("예정", "계획", "방침", "앞두고", "전망", "예상")
# 미래 날짜를 담아도 정상인 인용·결정 문장("~한다고 밝혔다", "~하기로 결정했다", "~할 것으로 예보했다")과
# 날짜를 목표로 정한 문장("8월 25일로 정했다")은 제외한다. v12 major 313건 실측에서 탐지 20건 중 10건이 이 유형의 오탐.
_REPORTED_OR_DECIDED_RE = re.compile(r"(다고|라고|기로|키로|것으로)\s|\d일로\s")
_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")


def _is_completed_predicate(sentence: str) -> bool:
    """문장 끝 서술어가 과거 완료형(받침 ㅆ + '다', 예: 했다·열렸다·됐다)인지 본다."""
    text = _TRAILING_PAREN_RE.sub("", sentence.rstrip(" .!?\"'”’"))  # "개막했다(현지시간)." 대응
    text = text.rstrip(" .!?\"'”’)")
    if len(text) < 2 or not text.endswith("다") or text.endswith(("있다", "겠다")):
        return False
    syllable = text[-2]
    return "가" <= syllable <= "힣" and (ord(syllable) - 0xAC00) % 28 == 20


def _find_future_completed_sentences(major: str, published_date: str) -> list[str]:
    """기사 작성일 이후 날짜를 예정 표현 없이 완료형으로 쓴 major 문장을 찾는다(예정→완료 변조 탐지)."""
    try:
        published = datetime.date.fromisoformat(published_date)
    except ValueError:
        return []
    flagged = []
    for sentence in re.split(r"(?<=[.!?。！？])\s+", " ".join(major.split())):
        if (
            any(word in sentence for word in _SCHEDULE_WORDS)
            or _REPORTED_OR_DECIDED_RE.search(sentence)
            or not _is_completed_predicate(sentence)
        ):
            continue
        for year, month, day in _FULL_DATE_RE.findall(sentence):
            try:
                if datetime.date(int(year), int(month), int(day)) > published:
                    flagged.append(sentence)
                    break
            except ValueError:
                continue
    return flagged


def _has_major(content: str) -> bool:
    return bool(str(_parse_json(content).get("major") or "").strip())


def _request_structure(client: LLMClient, prompt: str) -> dict:
    """구조화 응답을 받아 major/minor/entities를 정리한다. major가 비면 예외."""
    res = _parse_json(
        client.request(
            prompt,
            response_format={"type": "json_object"},
            temperature=0,
            cache_validator=_has_major,  # major가 빈 응답은 캐시하지 않는다(재시도가 같은 실패를 반복하지 않게)
        )
    )
    major = str(res.get("major") or "").strip()
    if not major:
        raise ValueError("LLM did not return major.")
    return {
        "major": major,
        "minor": [str(m).strip() for m in (res.get("minor") or []) if str(m).strip()],
        "entities": [str(e).strip() for e in (res.get("entities") or []) if str(e).strip()],
    }


def _extract_structure(client: LLMClient, source: str, published_date: str) -> tuple[dict, dict]:
    """기사를 major/minor/entities로 구조화한다.

    major가 작성일 이후의 일을 완료형으로 쓰면 해당 문장을 지적해 1회만 다시 요청하고,
    지적된 문장이 줄었을 때만 재요청 응답을 채택한다(정상 문장을 괜히 바꾸거나 entities가 흔들리지 않게).
    반환: (구조화 결과, 재요청 정보 {"flagged", "status", "after"})
      status: none(지적 없음) / fixed(재요청 채택) / still_flagged(줄지 않아 첫 응답 유지) / retry_failed(재요청 오류, 첫 응답 유지)
    """
    structure = _request_structure(client, build_structure_article_prompt(source, published_date))
    flagged = _find_future_completed_sentences(structure["major"], published_date)
    info = {"flagged": flagged, "status": "none", "after": []}
    if not flagged:
        return structure, info
    try:
        retried = _request_structure(
            client, build_structure_tense_retry_prompt(source, published_date, flagged)
        )
    except Exception as exc:
        print(f"[event] tense retry failed, keeping first response: {exc}", file=sys.stderr)
        info["status"] = "retry_failed"
        return structure, info
    info["after"] = _find_future_completed_sentences(retried["major"], published_date)
    if len(info["after"]) < len(flagged):
        info["status"] = "fixed"
        return retried, info
    info["status"] = "still_flagged"
    return structure, info


# articles 테이블의 벡터, 대표 사건(major), 부수 사건(minor) 데이터를 보완하는 쿼리
UPDATE_ARTICLE_ANALYSIS_SQL = """
UPDATE articles
SET embedding = ?::vector,
    core_content = ?,
    minor = ?::jsonb,
    updated_at = now()
WHERE id = ?
"""

# 기사에서 추출한 엔티티를 역색인 테이블에 적재하는 쿼리(자카드 회수 경로, 결정점 2=B).
# 테이블 PK가 (article_id, entity)라 LLM이 같은 엔티티를 두 번 내도 실패하지 않게 중복은 건너뛴다.
INSERT_ARTICLE_ENTITY_SQL = """
INSERT INTO article_entities (article_id, entity)
VALUES (?, ?)
ON CONFLICT DO NOTHING
"""

# 처리가 끝난 기사의 AI 분석 상태를 event_assigned로 변경하는 쿼리
UPDATE_AI_RESULT_STATUS_SQL = """
UPDATE article_ai_results
SET status = ?::public.article_ai_status,
    updated_at = now()
WHERE article_id = ?
"""


def process_event_classification(
    database_url: str | None,
    single_article_id: int | None = None,
    *,
    llm_model: str = LLM_MODEL,
) -> int:
    """분석 완료 기사들을 이벤트에 배정하거나 새 이벤트로 생성한다."""
    jev.require_api_key()  # 키가 없으면 모든 기사가 실패하므로 시작 전에 멈춘다.
    client = _get_client(llm_model)
    processed_count = 0

    with ensure_db("data/news.db", database_url=database_url) as conn:
        if single_article_id is not None:
            row = conn.query_one(
                """
                SELECT a.id, a.title, a.content, a.published_at, a.category,
                       a.article_image_url, a.bias_type, r.summary AS ai_summary
                FROM articles a
                JOIN article_ai_results r ON a.id = r.article_id
                WHERE a.id = ?
                """,
                (single_article_id,),
            )
            done_articles = [row] if row else []
        else:
            done_articles = conn.query(
                """
                SELECT a.id, a.title, a.content, a.published_at, a.category,
                       a.article_image_url, a.bias_type, r.summary AS ai_summary
                FROM articles a
                JOIN article_ai_results r ON a.id = r.article_id
                WHERE r.status = 'done'
                  AND a.content IS NOT NULL
                  AND trim(a.content) != ''
                ORDER BY a.published_at ASC
                LIMIT ?
                """,
                (BATCH_SIZE,),
            )

        print(f"[event] 처리 대상 기사: {len(done_articles)}건")

        for art in done_articles:
            art_id = art["id"]
            title = str(art["title"] or "").strip()
            full_content = art["content"]
            category = art["category"]
            img_url = art["article_image_url"]
            ai_summary = str(art["ai_summary"] or "").strip()

            published_date = str(art["published_at"] or "")[:10]
            trimmed_content = _get_first_sentences(full_content)
            representative_source = _build_representative_source(
                title,
                ai_summary,
                trimmed_content,
            )
            if not representative_source:
                continue

            try:
                # 기사를 major(핵심 사건)/minor(부수 사건)/entities(고유명사)로 구조화한다.
                structure, tense_info = _extract_structure(
                    client, representative_source, published_date
                )
                major = structure["major"]
                minor = structure["minor"]
                entities = structure["entities"]

                # 검색·저장 임베딩은 major로 계산한다(결정점 0, 2026-07-22 확정). 모델 최대 길이
                # (128토큰)를 넘는 뒷문장은 조용히 잘리지 않게 문장 단위로 미리 덜어낸다(첫 문장=핵심 사건은 유지).
                major_embedding = to_vector_literal(embed_query(fit_sentences_to_max_tokens(major)))
                candidates = events.search_candidate_events(
                    conn,
                    major_embedding,
                    entities,
                    CANDIDATE_POOL_SIZE,
                )
                article_embedding = major_embedding
                attempts = []
                presented_candidates = candidates
                jev_info = None
                if candidates:
                    # 배정 판단은 Jev가 후보 전체를 한 번에 채점한다(Jev 실패 시 예외 → 이 기사는
                    # 상태가 그대로 남아 다음 실행에서 다시 처리된다).
                    decision, jev_info = jev.decide_assignment(title, published_date, major, candidates)
                    attempts.append(
                        {
                            "pass": 1,
                            "candidate_event_ids": [int(c["id"]) for c in candidates],
                            "decision": decision,
                        }
                    )
                else:
                    decision = {
                        "action": "create",
                        "event_title": title,
                        "score": 0.0,
                        "reason": "검색 후보 없음",
                    }
                action = decision.get("action")
                if action not in {"assign", "create"}:
                    raise ValueError(f"Invalid event action: {action!r}")

                # rev3: 이벤트 요약 롤업은 토픽 분류 진입 시 1회만 한다(여기선 링크·카운트만).
                event_id = None
                final_event_id = None
                generated_title = None
                if action == "assign":
                    event_id = int(decision["event_id"])
                    candidate_ids = {int(candidate["id"]) for candidate in presented_candidates}
                    if event_id not in candidate_ids:
                        raise ValueError(f"Selected unknown event_id={event_id}.")
                else:
                    # 이벤트 이름은 헤드라인 대신 명사형으로 만들어 생성 시 고정한다(트랜잭션 밖 LLM 호출).
                    generated_title = generate_event_title(
                        client,
                        article_title=title,
                        major=major,
                        published_at=published_date,
                    )

                with conn.transaction():
                    # 기사 자체에도 같은 임베딩과 major/minor를 저장해 후속 분석에서 재사용한다.
                    conn.execute(
                        UPDATE_ARTICLE_ANALYSIS_SQL,
                        (
                            article_embedding,
                            major,
                            json.dumps(minor, ensure_ascii=False),
                            art_id,
                        ),
                    )
                    # entities는 역색인 테이블에 적재해 자카드 회수 경로로 사용한다.
                    for entity in entities:
                        conn.execute(INSERT_ARTICLE_ENTITY_SQL, (art_id, entity))

                    if action == "assign":
                        assert event_id is not None
                        reason = str(decision.get("reason") or "jev_event_assignment")
                        events.link_article_to_event(conn, event_id, art_id, reason)
                        final_event_id = event_id
                        msg = f"assigned to event {event_id}"
                    else:
                        new_id = events.create_new_event(
                            conn,
                            category=category,
                            title=generated_title,
                            summary=normalize_summary(ai_summary or major),
                            core_content=major,
                            embedding_text=representative_source,
                            embedding_literal=article_embedding,
                            event_image_url=img_url,
                        )
                        events.link_article_to_event(
                            conn,
                            new_id,
                            art_id,
                            str(decision.get("reason") or "new_event"),
                        )
                        final_event_id = new_id
                        msg = f"created event {new_id}"

                    conn.execute(UPDATE_AI_RESULT_STATUS_SQL, ("event_assigned", art_id))

                # Phase 0 진단: 후보 목록 + 최종 판정을 함께 기록(검색미스/판정오판 분해용).
                # 후보 내용(title 등)까지 저장해 DB 리셋 후에도 판정 리플레이가 가능하게 한다.
                presented_pass_by_id = {
                    event_id: attempt["pass"]
                    for attempt in attempts
                    for event_id in attempt["candidate_event_ids"]
                }
                log_candidates(
                    {
                        "stage": "event",
                        "article_id": art_id,
                        "published_at": str(art["published_at"]),
                        "published_date": published_date,
                        "article_title": title,
                        "ai_summary": ai_summary,
                        "major": major,
                        # 예정→완료 변조 재요청 정보(flagged=첫 응답에서 지적된 문장, status, after=재요청 응답의 지적 문장).
                        "tense_retry": tense_info,
                        "attempts": attempts,
                        "final_decision_pass": 1 if attempts else 0,
                        "candidates": [
                            {
                                "event_id": int(c["id"]),
                                "search_rank": rank,
                                "presented_pass": presented_pass_by_id.get(int(c["id"])),
                                "distance": float(c["distance"]),
                                "title": c["title"],
                                "core_content": c["core_content"],
                                "source_article_id": c["source_article_id"],
                                "source_core_content": c["source_core_content"],
                                "source_published_at": str(c["source_published_at"] or ""),
                                "summary": c["summary"],
                                "article_count": c["article_count"],
                                "earliest_published_at": str(c["earliest_published_at"]),
                            }
                            for rank, c in enumerate(candidates, 1)
                        ],
                        "action": action,
                        "final_event_id": final_event_id,
                        "score": load_decision_score(decision),
                        # 후보 순서(candidates)와 같은 순서의 Jev 확률. 판단 이유 대신 감사·디버깅에 쓴다.
                        "jev": jev_info,
                        "reason": str(decision.get("reason") or ""),
                    }
                )

                print(
                    json.dumps(
                        {
                            "article_id": art_id,
                            "major": major,
                            "result": msg,
                        },
                        ensure_ascii=False,
                    )
                )
                processed_count += 1

            except Exception as exc:
                print(f"[event] article {art_id} failed: {exc}", file=sys.stderr)
                continue

    return processed_count


def main() -> int:
    parser = argparse.ArgumentParser(description="분석 완료 기사를 이벤트에 배정(추출은 LLM, 배정 판단은 Jev)")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="Postgres/Supabase DB URL. 기본값은 DATABASE_URL 환경 변수",
    )
    parser.add_argument("--article-id", type=int, default=None, help="특정 기사 id 하나만 처리")
    parser.add_argument("--llm-model", default=LLM_MODEL, help="major/minor/entities 추출에 사용할 LLM 모델")
    args = parser.parse_args()

    if not args.database_url:
        print(
            "[event] Postgres connection is required. Set --database-url or DATABASE_URL.",
            file=sys.stderr,
        )
        return 1

    process_event_classification(
        database_url=args.database_url,
        single_article_id=args.article_id,
        llm_model=args.llm_model,
    )
    return 0
