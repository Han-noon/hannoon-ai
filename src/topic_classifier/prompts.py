"""토픽 분류 파이프라인용 LLM 프롬프트 빌더.

함수명은 이벤트 분류 프롬프트와 구분하기 위해 topic_ 접두사를 사용한다.
모든 응답은 JSON 객체로 반환되며, pipeline.py에서 json.loads()로 파싱된다.
"""

from db.topic_causes import TopicCandidate


def build_topic_cause_result_prompt(event_text: str) -> str:
    """이벤트 대표 기사(제목 + 첫 문단)에서 원인(cause)과 결과(result)를 추출하는 프롬프트를 생성한다.

    LLM 응답 형식: {"cause": "...", "result": "..."}
    """
    return f"""다음 뉴스 이벤트에서 핵심 '원인'과 '결과'를 각각 한 문장으로 추출하세요.

[이벤트]
{event_text}

[작성 규칙]
- 각 문장은 "[주체·지역] + [핵심 행위/사건] + [대상]" 구조를 따른다.
- 주체(기업·기관·인물)와 지역이 본문에 있으면 반드시 문장 앞부분에 명시하고, 없으면 생략한다.
- 문체는 '~다'체 평서문으로 통일하고, 수식어·비유·감정 표현을 제거해 핵심 사실만 남긴다.
- cause: 사건을 촉발한 직접적 사유. result: 그로 인해 발생한 상황/영향.
- 인과 관계가 분명하지 않은 사건도 cause와 result를 각각 반드시 한 문장씩 작성한다.

[예시]
{{"cause": "인천에서 빌라 임대인이 전세 보증금을 반환하지 못했다.", "result": "인천 빌라 세입자 30여 명이 보증금을 떼이는 피해를 입었다."}}

다음 JSON 형식으로만 응답하세요: {{"cause": "...", "result": "..."}}"""


def build_topic_assignment_prompt(
    title: str,
    summary: str,
    candidates: list[TopicCandidate],
) -> str:
    """이벤트를 기존 토픽에 배정하거나 새 토픽을 생성하도록 유도하는 프롬프트를 생성한다.

    LLM 응답 형식:
        배정: {"action": "assign", "topic_id": <정수>, "reason": "..."}
        생성: {"action": "create", "new_title": "<제목>", "reason": "..."}
    """
    candidates_str = _format_candidates(candidates)

    return f"""다음 뉴스 이벤트를 가장 적절한 기존 토픽에 배정하세요.
적절한 기존 토픽이 없다면 새 토픽을 생성하세요.

[대상 이벤트]
제목: {title}
요약: {summary}

[후보 토픽]
{candidates_str}

[배정 규칙]
1. 배정의 핵심 기준은 "같은 실제 사건(사안)인가"이다. 인물 이름 문자열이 아니라 지역·피해자·범행 방식·구체 사건 내용이 일치하는지로 판단한다.
2. 뉴스는 시간이 지남에 따라 같은 인물이 초기에는 익명("장모 씨", "A씨", "20대 남성")으로, 이후 실명으로 보도될 수 있다. 지역·피해자·사건 내용이 같다면 호칭 변화와 무관하게 동일 인물·동일 사건으로 간주한다.
3. 동일 사건에 대한 후속·전개 보도(예: 발생 → 검거 → 신상공개 → 송치 → 추가 혐의, 또는 심문 → 조정 → 파업)는 같은 토픽으로 묶는다.
4. 사건 유형이 비슷해도 지역·피해자·구체 사건 내용이 다르면 별도 토픽이다. 예: 광주 여고생 살해 사건과 서울 강서구 칼부림 사건은 둘 다 흉기 사건이지만 지역·피해자·가해자가 모두 달라 별개 토픽이다.
5. 위 1~4를 모두 만족하는 후보가 있을 때만 그 topic_id로 배정(assign)하고, 없으면 새 토픽을 생성(create)한다.
6. 새 토픽명은 '주체 + 핵심 사건' 형태로 간결하게 짓는다 (예: "삼성전자 노조 총파업").

다음 JSON 형식 중 하나로만 응답하세요. 위 필드 외 추가 필드를 포함하지 마세요.
배정 시: {{"action": "assign", "topic_id": <정수>, "reason": "배정 근거"}}
생성 시: {{"action": "create", "new_title": "<새 토픽 제목>", "reason": "신규 생성 근거"}}"""


def _format_candidates(candidates: list[TopicCandidate]) -> str:
    """TopicCandidate 목록을 프롬프트에 삽입할 텍스트 블록으로 변환한다."""
    if not candidates:
        return "(검색된 후보 없음)"

    parts = []
    for i, c in enumerate(candidates, 1):
        cause_lines = "\n".join(f"    - {ct}" for ct in c.cause_texts)
        parts.append(
            f"후보 {i} (topic_id: {c.topic_id})\n"
            f"  제목: {c.title}\n"
            f"  요약: {c.summary}\n"
            f"  관련 원인 문장들:\n{cause_lines}"
        )

    return "\n\n".join(parts)
