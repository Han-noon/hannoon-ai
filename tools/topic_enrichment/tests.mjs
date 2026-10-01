import test from "node:test";
import assert from "node:assert/strict";
import { enrich, refresh, VERSION, chunks } from "./core.mjs";
import { LocalQwen, SupabaseStore } from "./adapters.mjs";
const topic = (id) => ({
  id,
  title: `토픽 ${id}`,
  events: [
    {
      id: id + "01",
      title: "사건 제목",
      summary: "사건 내용",
      core_content: "",
    },
  ],
});
const snapshot = () => ({
  topics: [topic("1"), topic("2")],
  source_hash: "new",
  saved_hash: null,
  publication_version: 0,
});
function model() {
  return {
    identity: "test",
    preflight: async () => {},
    ask: async (kind, instruction, data) =>
      kind === "summary"
        ? {
            summary: "자료에 근거한 요약",
            keywords: ["핵심쟁점"],
            evidence_event_ids: data.events
              ? data.events.map((x) => x.id).slice(0, 12)
              : data.parts.flatMap((x) => x.evidence_event_ids).slice(0, 12),
          }
        : { related: false, reason: "", left_evidence: [], right_evidence: [] },
  };
}
test("모든 이벤트를 묶음 처리하고 빈 토픽은 초기화", async () => {
  const s = snapshot();
  s.topics[0].events = Array.from({ length: 20 }, (_, i) => ({
    id: String(1000 + i),
    title: "사건",
    summary: "가".repeat(1600),
  }));
  s.topics[1].events = [];
  const m = model(),
    ask = m.ask,
    seen = [];
  m.ask = (k, i, d, sc) => {
    if (d.events) seen.push(...d.events.map((x) => x.id));
    return ask(k, i, d, sc);
  };
  const out = await enrich(s, m);
  assert.equal(new Set(seen).size, 20);
  assert.deepEqual(out.topics[1], {
    id: "2",
    summary: "",
    keywords: [],
    evidence_event_ids: [],
  });
  assert.deepEqual(out.relations, []);
  assert.throws(
    () => chunks([{ summary: "가".repeat(5000) }]),
    /exceeds context/,
  );
});
test("변경 없는 입력은 생성 생략, 변경/모델 교체 시 재생성", async () => {
  let calls = 0;
  const s = snapshot(),
    m = model();
  const store = {
    snapshot: async () => s,
    publish: async () => {
      calls++;
      return { saved: true };
    },
  };
  s.saved_hash = s.source_hash;
  s.saved_model_version = VERSION + ":test";
  assert.equal(
    (await refresh({ store, model: m, topicIds: ["1", "2"] })).status,
    "unchanged",
  );
  s.source_hash = "edited";
  await refresh({ store, model: m, topicIds: ["1", "2"] });
  assert.equal(calls, 1);
});
test("미리보기와 생성 실패는 DB 저장하지 않음", async () => {
  let writes = 0;
  const store = {
    snapshot: async () => snapshot(),
    publish: async () => {
      writes++;
    },
  };
  assert.equal(
    (
      await refresh({
        store,
        model: model(),
        topicIds: ["1", "2"],
        dryRun: true,
      })
    ).status,
    "preview",
  );
  const m = model();
  m.ask = async () => {
    throw Error("generation failed");
  };
  await assert.rejects(refresh({ store, model: m, topicIds: ["1", "2"] }));
  assert.equal(writes, 0);
});
test("DB의 동시 갱신 거절을 성공으로 처리하지 않음", async () => {
  const store = {
    snapshot: async () => snapshot(),
    publish: async () => {
      throw Error("STALE_SNAPSHOT");
    },
  };
  await assert.rejects(
    refresh({ store, model: model(), topicIds: ["1", "2"] }),
    /STALE_SNAPSHOT/,
  );
});
test("Supabase 키는 서버 헤더로 전달, 오류 본문은 노출하지 않음", async () => {
  let request;
  const store = new SupabaseStore({
    url: "https://example.supabase.co",
    key: "sb_secret_test",
    fetchImpl: async (u, o) => {
      request = o;
      return { ok: true, json: async () => ({}) };
    },
  });
  await store.snapshot(["1"]);
  assert.equal(request.headers.apikey, "sb_secret_test");
  assert.equal(request.headers.Authorization, undefined);
  store.fetch = async () => ({
    ok: false,
    status: 400,
    json: async () => ({ code: "P0001", message: "secret private content" }),
  });
  await assert.rejects(
    store.snapshot(["1"]),
    (e) => !e.message.includes("private"),
  );
});
test("Qwen은 로컬 주소와 완결된 JSON 응답만 허용", async () => {
  assert.throws(
    () => new LocalQwen({ url: "https://remote.example" }),
    /local/,
  );
  const m = new LocalQwen({
    fetchImpl: async (u, o) => ({
      ok: true,
      json: async () =>
        u.endsWith("/api/tags")
          ? { models: [{ name: "qwen3:4b", digest: "abc" }] }
          : {
              done: true,
              done_reason: "length",
              model: "qwen3:4b",
              message: { content: "{}" },
            },
    }),
  });
  await m.preflight();
  await assert.rejects(m.ask("summary", "instruction", {}, {}), /Incomplete/);
});
