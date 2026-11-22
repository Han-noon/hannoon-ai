# Consolidation Membership Audit v1.8.8-periodic7

## 역할

현재 Subtopic의 Membership을 정리하는 cleanup audit다.

이 단계에서는 **Event Summary를 보지 않는다.**

입력으로 보는 Event 정보는 오직:

```text
event_id
event_name
```

이다.

이유:

> Event 핵심은 이미 event_name으로 확정되어 있으므로
> summary의 주변·배경 정보로 Event core를 다시 해석하지 않기 위해서다.

---

# 1. KEEP

다음 질문에 YES일 때만 KEEP한다.

> event_name 자체가 focal_basis를 직접 다루는가?

## FOCAL_POINT / RECURRING_ISSUE

event_name 자체의 핵심 대상·주체·제도·쟁점·문제가
focal_basis와 직접 일치해야 한다.

## PROCESS

event_name 자체가 동일한 하나의 구체 PROCESS 인스턴스의
실제 단계·진행·결과를 직접 나타내야 한다.

---

# 2. REMOVE

다음이면 REMOVE한다.

- event_name 핵심이 다른 사건이다.
- Subtopic 관련 내용은 원래 summary를 봐야만 알 수 있다.
- 같은 Topic이라 관련 있을 뿐이다.
- 같은 기관·주체가 등장할 뿐이다.
- 같은 넓은 목표를 공유할 뿐이다.
- 원인·대응·결과 관계라는 설명이 있어야 연결된다.
- "이해하는 데 도움이 되는 정보"일 뿐 필터 자체가 아니다.
- PROCESS인데 다른 절차 인스턴스다.

애매하면 REMOVE한다.

---

# 3. 핵심 역검사

각 Event마다 다음 문장을 채운다.

> "`event_name`의 핵심은 바로 `[focal_basis]`를 다룬다."

자연스럽게 성립하면 KEEP.
별도 설명이 필요하면 REMOVE.

---

# 4. 출력

입력 Event마다 정확히 한 번 판정한다.

```json
{
  "decisions": [
    {"event_id": "EV_A", "decision": "KEEP"},
    {"event_id": "EV_B", "decision": "REMOVE"}
  ]
}
```

설명문 없이 JSON 객체 하나만 반환한다.
