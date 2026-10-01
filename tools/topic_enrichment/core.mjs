import crypto from "node:crypto";
import { findRelations } from "./relations.mjs";
export const VERSION = "topic-enrichment-v2";
export const hash = (x) =>
  crypto.createHash("sha256").update(JSON.stringify(x)).digest("hex");
export function id(x) {
  if (
    typeof x !== "string" ||
    !/^[1-9]\d*$/.test(x) ||
    BigInt(x) > 9223372036854775807n
  )
    throw Error("Invalid string ID");
  return x;
}
function text(x, max) {
  if (typeof x !== "string" || !x.trim() || x.length > max)
    throw Error("Invalid generated text");
  return x.trim();
}
export function validateSummary(x, allowed) {
  if (
    !x ||
    !Array.isArray(x.keywords) ||
    x.keywords.length < 1 ||
    x.keywords.length > 5 ||
    !Array.isArray(x.evidence_event_ids) ||
    !x.evidence_event_ids.length ||
    x.evidence_event_ids.length > 12
  )
    throw Error("Invalid summary result");
  x.summary = text(x.summary, 1200);
  x.keywords = x.keywords.map((k) => text(k, 30).replace(/^#+/, ""));
  if (
    x.keywords.some((k) => !k) ||
    new Set(x.keywords).size !== x.keywords.length
  )
    throw Error("Duplicate/empty keyword");
  if (x.evidence_event_ids.some((k) => !allowed.has(id(k))))
    throw Error("Unknown summary evidence");
  return {
    summary: x.summary,
    keywords: x.keywords,
    evidence_event_ids: [...new Set(x.evidence_event_ids)],
  };
}
export const summarySchema = {
  type: "object",
  additionalProperties: false,
  required: ["summary", "keywords", "evidence_event_ids"],
  properties: {
    summary: { type: "string" },
    keywords: {
      type: "array",
      items: { type: "string" },
      minItems: 1,
      maxItems: 5,
    },
    evidence_event_ids: {
      type: "array",
      items: { type: "string" },
      minItems: 1,
      maxItems: 12,
    },
  },
};
const SUMMARY =
  "주어진 뉴스 이벤트를 종합하여 한국어 카드 요약(1~3문장, 최대 400자)과 핵심 키워드 1~5개를 작성하세요. 키워드는 # 없이 짧게. 원인·쟁점·확인된 진행 상황을 구분하세요. 자료에 없는 사실, 날짜, 인과관계를 만들지 마세요. 사건 시점은 본문에 명시된 날짜만 사용하세요. 토픽 제목만으로 채우지 말고 모든 입력 묶음을 고려하세요. 서로 다른 사건을 하나라고 단정하지 마세요. 짧은 자료는 한 문장으로 끝내세요. 문장 수를 채우기 위해 배경지식·의미 해석·목적·전망·정부 대응을 보태지 마세요. 입력에 명시된 사실만 압축하며 입력에 없는 증가·감소·초기 단계 등의 판단도 금지합니다. 키워드 역시 입력에 나온 개념만 사용하세요. 근거로 사용한 event ID를 evidence_event_ids에 기록하세요.";

export function chunks(items, maxBytes = 12000) {
  const groups = [];
  let group = [];
  for (const item of items) {
    if (Buffer.byteLength(JSON.stringify([item])) > maxBytes)
      throw Error(
        "One event exceeds context budget; shorten its upstream summary explicitly",
      );
    if (
      group.length &&
      Buffer.byteLength(JSON.stringify([...group, item])) > maxBytes
    ) {
      groups.push(group);
      group = [];
    }
    group.push(item);
  }
  if (group.length) groups.push(group);
  return groups;
}
export async function enrich(snapshot, model) {
  if (
    !snapshot ||
    !Array.isArray(snapshot.topics) ||
    !snapshot.topics.length ||
    snapshot.topics.length > 10
  )
    throw Error("Invalid topic snapshot");
  const seen = new Set(),
    eventSeen = new Set(),
    summaries = [];
  for (const topic of snapshot.topics) {
    if (seen.has(id(topic.id))) throw Error("Duplicate topic");
    seen.add(topic.id);
    if (!Array.isArray(topic.events)) throw Error("Invalid events");
    for (const e of topic.events) {
      if (eventSeen.has(id(e.id))) throw Error("Duplicate event");
      eventSeen.add(e.id);
    }
    if (!topic.events.length) {
      summaries.push({
        id: topic.id,
        summary: "",
        keywords: [],
        evidence_event_ids: [],
      });
      continue;
    }
    let pieces = chunks(topic.events).map((events) => ({
      title: topic.title,
      events,
    }));
    let level = 0;
    while (true) {
      const next = [];
      for (const piece of pieces) {
        const allowed = new Set(
          piece.events
            ? piece.events.map((e) => e.id)
            : piece.parts.flatMap((p) => p.evidence_event_ids),
        );
        const schema = structuredClone(summarySchema);
        schema.properties.evidence_event_ids.items.enum = [...allowed];
        next.push(
          validateSummary(
            await model.ask("summary", SUMMARY, piece, schema),
            allowed,
          ),
        );
      }
      if (next.length === 1) {
        summaries.push({ id: topic.id, ...next[0] });
        break;
      }
      if (++level > 10) throw Error("Summary reduction did not converge");
      pieces = chunks(next, 12000).map((parts) => ({
        title: topic.title,
        parts,
      }));
      if (pieces.length >= next.length)
        throw Error("Summary reduction too large");
    }
  }
  const { relations, diagnostics } = await findRelations(
    snapshot.topics,
    model,
  );
  return { topics: summaries, relations, diagnostics };
}

export async function refresh({
  store,
  model,
  topicIds,
  dryRun = false,
  force = false,
}) {
  if (
    !Array.isArray(topicIds) ||
    !topicIds.length ||
    topicIds.length > 10 ||
    new Set(topicIds.map(id)).size !== topicIds.length
  )
    throw Error("Configure 1 to 10 unique numeric string DB IDs");
  await model.preflight();
  const snapshot = await store.snapshot(topicIds);
  const modelVersion = VERSION + ":" + model.identity;
  if (
    !force &&
    snapshot.source_hash === snapshot.saved_hash &&
    snapshot.saved_model_version === modelVersion
  )
    return { status: "unchanged" };
  const output = await enrich(snapshot, model);
  if (dryRun)
    return { status: "preview", source_hash: snapshot.source_hash, ...output };
  const saved = await store.publish({
    p_topic_ids: topicIds,
    p_source_hash: snapshot.source_hash,
    p_publication_version: snapshot.publication_version,
    p_model_version: modelVersion,
    p_topics: output.topics,
    p_relations: output.relations,
  });
  return { status: "saved", ...saved };
}
