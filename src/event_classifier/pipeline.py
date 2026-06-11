"""이벤트 분류 파이프라인"""

import json
import re
import sys
from collector.storage import ensure_db
from openai_client.client import OpenAIClient
from embedding import embed 

from db import events
from event_classifier.settings import (
    BATCH_SIZE,
    TOP_K,
    DISTANCE_THRESHOLD,
    FINAL_THRESHOLD,
    LLM_MODEL,
)
from event_classifier.prompts import (
    build_extract_main_event_prompt, 
    build_verify_event_prompt,
    build_generate_event_title_prompt
)

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
    """기사 본문에서 마침표(.)를 기준으로 4문장(한문단)만 사용한다."""
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
    """
    article_ai_results 테이블의 상태가 'done'인 기사를 이벤트에 분류하고, 
    처리가 완료되면 상태를 'event_assigned'로 변경한다.
    """
    client = _get_client()
    processed_count = 0

    with ensure_db("data/news.db", database_url=database_url) as conn:
        if single_article_id is not None:
            row = conn.query_one(
                """
                SELECT a.id, a.content, a.published_at, a.category, a.article_image_url, a.bias_type, r.abuse_label
                FROM articles a
                JOIN article_ai_results r ON a.id = r.article_id
                WHERE a.id = ?
                """, (single_article_id,)
            )
            done_articles = [row] if row else []
        else:
            # 상태가 'done'인 대상 기사들을 가져온다.
            done_articles = conn.query(
                """
                SELECT a.id, a.content, a.published_at, a.category, a.article_image_url, a.bias_type, r.abuse_label
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

        print(f"[event] 처리 대상 기사: {len(done_articles)}건")

        for art in done_articles:
            art_id = art["id"]
            full_content = art["content"]
            pub_date_str = str(art["published_at"])
            category = art["category"]
            img_url = art["article_image_url"]
            abuse_label = art["abuse_label"]

            # 어뷰징 여부 사전 판정
            is_abusing = True if abuse_label == "abuse" else False

            if not full_content:
                continue

            # 본문에서 4문장만 추출
            trimmed_content = _get_first_four_sentences(full_content)
            print(f"처리 대상 기사 전처리: {trimmed_content}")
            if not trimmed_content:
                continue

            try:
                # 1. 대표 사건 추출
                prompt_extract = build_extract_main_event_prompt(trimmed_content)
                res_extract = _parse_json(client.request(prompt_extract, response_format={"type": "json_object"}))
                main_event = res_extract.get("main_event", "")

                # 2. 임베딩(벡터) 추출
                art_embedding = embed(trimmed_content)

                # 3. 이벤트 탐색
                candidate_list = events.search_candidate_events(
                    conn, art_embedding, pub_date_str, max_distance=DISTANCE_THRESHOLD, top_k=TOP_K
                )

                best_event = None
                best_score = 0

                for candidate in candidate_list:
                    sim_score = 1 - candidate["distance"] if candidate["distance"] is not None else 0
                    
                    # 4. 동일성 정밀 비교 검증
                    prompt_verify = build_verify_event_prompt(main_event, candidate["core_content"])
                    res_verify = _parse_json(client.request(prompt_verify, response_format={"type": "json_object"}))
                    llm_score = res_verify.get("score", 0)

                    final_score = (llm_score * 0.7) + (sim_score * 0.3)
                    if final_score > best_score:
                        best_score = final_score
                        best_event = candidate

                # 5. 단일 기사 단위 Commit/Rollback 트랜잭션 구역
                with conn.transaction():
                    # 5.1. articles 테이블 데이터 보완 업데이트
                    conn.execute(UPDATE_ARTICLE_ANALYSIS_SQL, (art_embedding, main_event, art_id))

                    # 5.2. 최종 판정 기준 통과 유무 체크 및 이벤트 매핑
                    if best_score >= FINAL_THRESHOLD:
                        if is_abusing:
                            events.update_event_counters(conn, event_id=best_event["id"], article_id=art_id, is_abusing=True)

                        events.link_article_to_event(
                            conn, best_event["id"], art_id, 
                            f"best_score: ({best_score:.4f}) | 어뷰징유무: {is_abusing}", 
                            is_abusing=is_abusing
                        )
                        msg = f"기존 이벤트 {best_event['id']}번에 귀속"
                    else:
                        print(f"[LLM] 신규 이벤트 생성을 위한 제목 추출 시작 (기사 ID: {art_id})")
                        prompt_title = build_generate_event_title_prompt(trimmed_content)
                        res_title = _parse_json(client.request(prompt_title, response_format={"type": "json_object"}))
                        generated_title = res_title.get("event_title", main_event)

                        new_id = events.create_new_event(
                            conn, 
                            category=category, 
                            title=generated_title,     
                            summary="",                 
                            core_content=main_event, 
                            embedding_text=trimmed_content, 
                            embedding=art_embedding, 
                            event_image_url=img_url
                        )

                        if is_abusing:
                            events.update_event_counters(conn, event_id=new_id, article_id=art_id, is_abusing=True)

                        events.link_article_to_event(conn, new_id, art_id, "", is_abusing=is_abusing)
                        msg = f"신규 이벤트 {new_id}번 생성"

                    # 5.3. 기사 처리가 완료 -> AI 분석 결과 상태 'event_assigned'로 변경
                    conn.execute(UPDATE_AI_RESULT_STATUS_SQL, ('event_assigned', art_id))

                print(f"\n===========================================")
                print(f"처리된 기사 ID: {art_id} (라벨: {abuse_label})")
                print(f"판정 결과: {msg}")
                print(f"best_score: {best_score:.4f}")
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