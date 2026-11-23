# Consolidation Display-Name Repair v1.8.8-periodic7

## 역할

Membership Audit이 끝난 group의 **display name과 type만** 정리한다.

Subtopic의 존재, Membership, common_basis, audit_criterion은 바꾸지 않는다.

완벽한 이름을 만들기보다 사용자가 알아볼 수 있는 짧은 필터명을 만든다.

---

# name

- 하나의 구체 대상 / 제도 / 쟁점 / 절차 / 개체 / 역할을 우선
- 1~6어절 정도의 짧은 명사구
- Event 여러 개의 행동을 문장처럼 요약하지 않음
- focal_basis와 의미가 같으면 exact substring일 필요 없음

다음 병렬 요약형은 피한다.

- `[사건 A]와 [사건 B]`
- `[사건 A] 및 [사건 B]`
- `[행동] + [결과]`
- `[쟁점] + [책임]`

다만 **하나의 제도·정책·고정 개념을 이루는 짧은 복합 명칭**은
가운데점 `·`을 사용할 수 있다.

가운데점이 있다는 이유만으로 이름을 한쪽만 남기지 않는다.

또한 `합의`, `결렬`, `요구`, `체결`, `부결`, `가결`, `재개` 같은 말도
그 상태 자체가 필터의 핵심이면 사용할 수 있다.
단순히 여러 Event의 흐름을 요약하기 위해 붙이지 않는다.

---

# type

- FOCAL_POINT
- PROCESS
- RECURRING_ISSUE

PROCESS는 실제로 하나의 동일한 구체 절차 인스턴스일 때만 사용한다.
확신이 없으면 FOCAL_POINT 또는 RECURRING_ISSUE를 사용한다.

---

# 출력

```json
{
  "labels": [
    {
      "group_key": "G001",
      "name": "[짧은 필터 라벨]",
      "type": "FOCAL_POINT"
    }
  ]
}
```

설명문 없이 JSON 객체 하나만 반환한다.
