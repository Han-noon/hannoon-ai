"""이벤트 분류 파이프라인 코어 엔진."""

import json
import re
import sys
from collector.storage import ensure_db
from openai_client.client import OpenAIClient
from embedding import embed  # 팀 공통 임베딩 유틸

from db import events
from event_classifier.settings import (
    BATCH_SIZE,
    TOP_K,
    DISTANCE_THRESHOLD,
    FINAL_THRESHOLD,
    LLM_MODEL,
)
from event_classifier.prompts import build_extract_main_event_prompt, build_verify_event_prompt

_client = None

def _get_client() -> OpenAIClient:
    global _client
    if _client is None:
        _client = OpenAIClient(model=LLM_MODEL)
    return _client

def _parse_json(response: str) -> dict:
    response = response.strip()
    try:
        return json.loads(response)
    except Exception:
        match = re.search(r"\{.*\}", response, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise ValueError("JSON 구조를 해석할 수 없습니다.")

def _get_first_four_sentences(text: str) -> str:
    """기사 본문에서 마침표(.)를 기준으로 딱 4문장까지만 잘라서 반환하는 헬퍼 함수"""
    if not text:
        return ""
    sentences = re.split(r'\.(?=\s|$)', text)
    sentences = [s.strip() for s in sentences if s.strip()]
    first_four = sentences[:4]
    return ". ".join(first_four) + "."

# articles 테이블의 벡터 및 대표 사건 데이터를 보완하는 쿼리
UPDATE_ARTICLE_ANALYSIS_SQL = """
UPDATE articles 
SET embedding = ?::vector, 
    core_content = ?,
    updated_at = now()
WHERE id = ?
"""

# 처리가 끝난 기사의 AI 분석 상태를 event_assigned로 변경하는 쿼리
UPDATE_AI_RESULT_STATUS_SQL = """
UPDATE article_ai_results
SET status = ?::public.article_ai_status,
    updated_at = now()
WHERE article_id = ?
"""

def process_event_classification(database_url: str | None, single_article_id: int | None = None) -> int:
    """article_ai_results 테이블의 상태가 'done'인 기사를 분류하고, 
    처리가 완료되면 상태를 'event_assigned'로 변경합니다.
    """
    client = _get_client()
    processed_count = 0

    with ensure_db("data/news.db", database_url=database_url) as conn:
        if single_article_id is not None:
            row = conn.query_one("SELECT id, content, published_at, category FROM articles WHERE id = ?", (single_article_id,))
            ready_articles = [row] if row else []
        else:
            # 상태가 'done'인 대상 기사들을 긁어옵니다.
            ready_articles = conn.query(
                """
                SELECT a.id, a.content, a.published_at, a.category 
                FROM articles a
                JOIN article_ai_results r ON a.id = r.article_id
                WHERE r.status = 'done' 
                  AND a.content IS NOT NULL 
                  AND a.content != ''
                ORDER BY a.published_at ASC 
                LIMIT ?
                """, 
                (BATCH_SIZE,)
            )

        print(f"[event] 처리 대상 기사: {len(ready_articles)}건")

        for art in ready_articles:
            art_id = art["id"]
            full_content = art["content"]
            pub_date_str = str(art["published_at"])
            category = art["category"]

            if not full_content:
                continue

            # 본문에서  4문장만 정밀 추출
            trimmed_content = _get_first_four_sentences(full_content)
            print(f"처리 대상 기사 전처리: {trimmed_content}")
            if not trimmed_content:
                continue

            try:
                # Step 1. 대표 사건 추출
                prompt_extract = build_extract_main_event_prompt(trimmed_content)
                res_extract = _parse_json(client.request(prompt_extract, response_format={"type": "json_object"}))
                main_event = res_extract.get("main_event", "")

                # Step 2. 임베딩(벡터) 추출
                art_embedding = embed(trimmed_content)

                # Step 3. 주변 이벤트 탐색
                candidate_list = events.search_candidate_events(
                    conn, art_embedding, pub_date_str, max_distance=DISTANCE_THRESHOLD, top_k=TOP_K
                )

                best_event = None
                best_score = 0

                for candidate in candidate_list:
                    sim_score = 1 - candidate["distance"] if candidate["distance"] is not None else 0
                    
                    # Step 4. 동일성 정밀 비교 검증
                    prompt_verify = build_verify_event_prompt(main_event, candidate["core_content"])
                    res_verify = _parse_json(client.request(prompt_verify, response_format={"type": "json_object"}))
                    llm_score = res_verify.get("score", 0)

                    final_score = (llm_score * 0.7) + (sim_score * 0.3)
                    if final_score > best_score:
                        best_score = final_score
                        best_event = candidate

                # Step 5. 단일 기사 단위 Commit/Rollback 트랜잭션 구역
                with conn.transaction():
                    # 5-1. articles 테이블 데이터 보완 업데이트
                    conn.execute(UPDATE_ARTICLE_ANALYSIS_SQL, (art_embedding, main_event, art_id))

                    # 5-2. 최종 판정 기준 통과 유무 체크 및 이벤트 매핑
                    if best_score >= FINAL_THRESHOLD:
                        events.update_event_deadline(conn, best_event["id"], pub_date_str)
                        events.link_article_to_event(conn, best_event["id"], art_id, f"결합 점수 통과 충족 ({best_score:.4f})")
                        msg = f"기존 이벤트 {best_event['id']}번에 귀속"
                    else:
                        temp_title = main_event
                        new_id = events.create_new_event(
                            conn, category, temp_title, main_event, main_event, trimmed_content, art_embedding
                        )
                        events.link_article_to_event(conn, new_id, art_id, "기준치 미달로 독립 이벤트 개설")
                        msg = f"신규 이벤트 {new_id}번 생성"

                    # 5-3. [핵심 추가] 기사 처리가 완전히 성공했으므로 AI 분석 결과 상태를 'event_assigned'로 변경!
                    conn.execute(UPDATE_AI_RESULT_STATUS_SQL, ('event_assigned', art_id))

                print(f"\n===========================================")
                print(f"처리된 기사 ID: {art_id}")
                print(f"판정 결과: {msg}")
                print(f"융합 최고 점수: {best_score:.4f}")
                print(f" 상태 변경: done -> event_assigned")
                print(f"===========================================\n")
                processed_count += 1

            except Exception as e:
                print(f"[event] 기사 {art_id} 파이프라인 처리 에러 격리: {e}", file=sys.stderr)
                continue

    return processed_count

def main() -> int:
    import os
    database_url = os.environ.get("DATABASE_URL")
    process_event_classification(database_url=database_url)
    return 0