import test from "node:test";
import assert from "node:assert/strict";
import { findRelations } from "./relations.mjs";
import { enrich, refresh, VERSION } from "./core.mjs";
const topics = () => [
  {
    id: "17",
    title: "하천 오염",
    events: [
      {
        id: "101",
        title: "청명강 오염으로 상수도 취수 중단",
        summary: "청명강 오염 때문에 주민 급수가 중단됐다.",
      },
    ],
  },
  {
    id: "29",
    title: "주민 급수",
    events: [
      {
        id: "202",
        title: "청명강 오염으로 주민 급수 중단",
        summary: "청명강 오염 때문에 주민 급수가 중단됐다.",
      },
    ],
  },
];
const no = {
  related: false,
  reason: "",
  left_evidence: [],
  right_evidence: [],
};
function mock() {
  return {
    identity: "fixture",
    preflight: async () => {},
    ask: async (k, i, d) => {
      if (k === "summary")
        return {
          summary: "자료 요약",
          keywords: ["오염"],
          evidence_event_ids: (d.events || []).map((e) => e.id),
        };
      if (k === "relation_verify")
        return {
          supported: true,
          related: true,
          left_key: d.left.excerpts[0].key,
          right_key: d.right.excerpts[0].key,
        };
      return {
        related: true,
        reason:
          "연결 근거: “청명강 오염 때문에 주민 급수가 중단됐다.” / “청명강 오염 때문에 주민 급수가 중단됐다.”",
        left_evidence: [
          { event_id: d.left.events[0].id, quote: d.left.events[0].title },
        ],
        right_evidence: [
          { event_id: d.right.events[0].id, quote: d.right.events[0].title },
        ],
      };
    },
  };
}
test("임의 주제/ID에서도 원본 확인 후 기존 DB 형식으로 연결", async () => {
  const x = await findRelations(topics(), mock());
  assert.deepEqual(x.relations, [
    {
      topic_id: "17",
      related_topic_id: "29",
      reason:
        "연결 근거: “청명강 오염 때문에 주민 급수가 중단됐다.” / “청명강 오염 때문에 주민 급수가 중단됐다.”",
      source_event_ids: ["101"],
      target_event_ids: ["202"],
    },
  ]);
});
test("명시적 무관계는 빈 결과", async () => {
  const m = mock();
  m.ask = async () => no;
  assert.deepEqual((await findRelations(topics(), m)).relations, []);
});
test("상세 원문이 연결을 뒷받침하지 않으면 제외", async () => {
  const m = mock(),
    ask = m.ask;
  m.ask = (k, ...args) =>
    k === "relation_verify"
      ? { supported: false, related: false, left_key: "", right_key: "" }
      : ask(k, ...args);
  assert.equal((await findRelations(topics(), m)).relations.length, 0);
});
test("다른 토픽의 ID는 한 번 재시도하고 수정된 응답만 사용", async () => {
  const m = mock(),
    ask = m.ask;
  let n = 0;
  m.ask = async (k, ...args) => {
    const x = await ask(k, ...args);
    if (k === "relation_pair" && n++ === 0) x.left_evidence[0].event_id = "202";
    return x;
  };
  const x = await findRelations(topics(), m);
  assert.equal(n, 2);
  assert.equal(x.relations.length, 1);
  assert.equal(x.diagnostics[0].retried, true);
});
test("날조된 제목 인용은 반복돼도 통과하지 않음", async () => {
  const m = mock(),
    ask = m.ask;
  m.ask = async (...args) => {
    const x = await ask(...args);
    if (args[0] === "relation_pair")
      x.left_evidence[0].quote = "원문에 없는 역사적 사건 관련 주장";
    return x;
  };
  await assert.rejects(findRelations(topics(), m), /after retry/);
});
test("검수 단계의 다른 쪽 근거 key도 거부", async () => {
  const m = mock(),
    ask = m.ask;
  m.ask = async (k, ...args) => {
    const x = await ask(k, ...args);
    if (k === "relation_verify") x.left_key = x.right_key;
    return x;
  };
  await assert.rejects(findRelations(topics(), m), /after retry/);
});
test("false 판정인데 근거가 붙으면 재시도 후 거부", async () => {
  const m = mock();
  m.ask = async () => ({ ...no, reason: "연관 있음" });
  await assert.rejects(findRelations(topics(), m), /after retry/);
});
test("생성 요약은 관계 판단에 들어가지 않음", async () => {
  const m = mock(),
    ask = m.ask;
  m.ask = (k, i, d, s) => {
    if (k !== "summary") assert.ok(!JSON.stringify(d).includes("날조요약"));
    return k === "summary"
      ? {
          summary: "날조요약",
          keywords: ["오염"],
          evidence_event_ids: d.events.map((e) => e.id),
        }
      : ask(k, i, d, s);
  };
  assert.equal((await enrich({ topics: topics() }, m)).relations.length, 1);
});
test("긴 입력도 모든 이벤트 제목을 비교하고 큰 단일 입력은 오류", async () => {
  const t = topics();
  t[0].events = Array.from({ length: 30 }, (_, i) => ({
    id: String(1000 + i),
    title: "오염 관련 뉴스 제목 " + String(i) + "가".repeat(500),
  }));
  const seen = new Set(),
    m = mock();
  m.ask = async (k, i, d) => {
    d.left.events.forEach((e) => seen.add(e.id));
    assert.ok(Buffer.byteLength(JSON.stringify(d)) < 24000);
    return no;
  };
  await findRelations(t, m);
  assert.equal(seen.size, 30);
  t[0].events[0].title = "가".repeat(4000);
  await assert.rejects(findRelations(t, m), /too large/);
});
test("10개 토픽 45쌍 비교, 카드당 최대 3개, 중복 없음", async () => {
  const t = Array.from({ length: 10 }, (_, i) => ({
    id: String(i + 1),
    title: "토픽",
    events: [{ id: String(100 + i), title: "청명강 오염으로 주민 급수 중단" }],
  }));
  const m = mock(),
    ask = m.ask,
    seen = new Set();
  m.ask = (k, i, d, s) => {
    if (k === "relation_pair") seen.add(d.left.id + ":" + d.right.id);
    return ask(k, i, d, s);
  };
  const x = await findRelations(t, m);
  assert.equal(seen.size, 45);
  assert.equal(
    new Set(x.relations.map((r) => r.topic_id + ":" + r.related_topic_id)).size,
    x.relations.length,
  );
  for (const v of t)
    assert.ok(
      x.relations.filter(
        (r) => r.topic_id === v.id || r.related_topic_id === v.id,
      ).length <= 3,
    );
});
test("근거 검증 실패 시 저장은 0회, 미리보기와 버전 캐시 유지", async () => {
  let writes = 0;
  const s = {
    topics: topics(),
    source_hash: "hash",
    saved_hash: "hash",
    saved_model_version: VERSION + ":fixture",
  };
  const store = {
    snapshot: async () => s,
    publish: async () => {
      writes++;
      return { saved: true };
    },
  };
  const m = mock();
  assert.equal(
    (await refresh({ store, model: m, topicIds: ["17", "29"] })).status,
    "unchanged",
  );
  s.saved_hash = null;
  assert.equal(
    (await refresh({ store, model: m, topicIds: ["17", "29"], dryRun: true }))
      .status,
    "preview",
  );
  m.ask = async () => {
    throw Error("generation failed");
  };
  await assert.rejects(refresh({ store, model: m, topicIds: ["17", "29"] }));
  assert.equal(writes, 0);
});
