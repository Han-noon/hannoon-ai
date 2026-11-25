# Subtopic classifier state management

## 결론

현재 공개 DB 테이블(`events`, `event_profiles`, `subtopics`,
`subtopic_events`)만으로는 17_78 스트림 state를 정확히 복원할 수 없다.
Event/Profile/Subtopic/membership의 현재 결과는 복원할 수 있지만, 실행 시점을
결정하는 이력은 저장되어 있지 않다. 따라서 Event 수로 checkpoint를 추정해서는
안 된다.

상태 저장소가 도입되기 전 일반 배치는 기존 처리 state가 있는 Topic의 증분 처리를
중단하고 `ClassifierStateUnavailableError`를 보고한다. 전체 재분류는 사용자가
`--replay-topic --topic-id ID`를 명시한 경우에만 실행한다.

## 후속 분류에 필요한 state

코어가 다음 Event를 처리하기 위해 직접 사용하는 최소 상태는 다음과 같다.

- `topic_name`
- `processed_event_ids`, `last_processed_event_id`, `last_processed_step`
- 활성 `subtopics` 전체
  - `subtopic_id`, 이름, 유형, `common_basis`, `membership_criterion`
  - `event_ids`
  - `created_at_step`
  - creation/update/refinement/consolidation 이력
- `next_subtopic_number`
- `last_consolidated_step`, `consolidation_count`
- `retired_subtopics`
- DB Subtopic ID와 코어 Subtopic ID의 안정적인 매핑

특히 `created_at_step`은 마지막 consolidation 이후 새 Subtopic 3개가 생성되었는지
판단하는 조기 consolidation 조건에 사용된다. `last_consolidated_step`이 달라지면
정규 consolidation과 그 직후 recovery 시점도 달라진다.

## 현재 스키마에서 복원 가능한 정보

- Topic 이름과 Event의 처리 완료 여부
- 유효한 Event Profile과 시간순 입력
- 활성 Subtopic의 현재 정의
- 현재 Subtopic membership
- DB Subtopic ID

다음 정보는 정확히 복원할 수 없다.

- Subtopic이 생성된 stream step
- 마지막 consolidation의 정확한 step과 누적 횟수
- retired/merged/dropped Subtopic 이력
- membership validation 및 refinement 이력
- recovery가 적용된 checkpoint
- 코어 Subtopic ID와 DB ID의 과거 매핑

## 권장 상태 테이블

코어의 미래 필드 추가에도 대응할 수 있도록 필드를 잘게 분해하기보다 검증된 state
JSON 전체를 원자적으로 저장하는 방식을 권장한다.

```sql
CREATE TABLE public.subtopic_classifier_states (
  topic_id bigint PRIMARY KEY REFERENCES public.topics(id) ON DELETE CASCADE,
  pipeline_version text NOT NULL,
  state jsonb NOT NULL,
  state_status text NOT NULL CHECK (
    state_status IN ('ready', 'rebuild_required')
  ),
  created_at timestamp without time zone NOT NULL DEFAULT now(),
  updated_at timestamp without time zone NOT NULL DEFAULT now()
);
```

`state`에는 17_78 코어 state와 `db_subtopic_ids`, `last_processed_order_key`를 함께
저장한다. `pipeline_version`은 코어·분류 프롬프트·Schema 묶음의 고정 버전이어야
한다.

Event 한 건을 저장할 때 다음 항목을 하나의 transaction에서 처리해야 한다.

1. Subtopic 생성·수정·삭제
2. membership 동기화
3. classifier state UPSERT
4. 마지막으로 `events.subtopic_processed_at` 갱신

어느 단계든 실패하면 네 변경을 모두 rollback한다.

## 기존 Topic 이관

기존 Topic은 현재 테이블만으로 state를 만들어 `ready`로 표시하면 안 된다. 다음
우선순위를 권장한다.

1. 과거 17_78 결과의 `subtopic_state.json`이 있으면 DB ID 매핑을 검증한 후 import
2. 원본 state가 없으면 `rebuild_required`로 등록
3. 사용자가 명시적으로 승인한 Topic만 전체 시간순 replay
4. replay 결과와 공개 Subtopic 테이블을 같은 transaction으로 저장한 뒤 `ready` 전환

처리 Event 수로 `last_consolidated_step`이나 `consolidation_count`를 계산하는 방식은
이관으로 간주하지 않는다.
