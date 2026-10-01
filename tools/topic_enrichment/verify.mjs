import fs from "node:fs/promises";
import { enrich, hash, VERSION } from "./core.mjs";
import { LocalQwen } from "./adapters.mjs";
const [inputPath, outputPath] = process.argv.slice(2);
if (!inputPath || !outputPath)
  throw Error("Usage: node verify.mjs snapshot.json output.json");
const snapshot = JSON.parse(await fs.readFile(inputPath, "utf8"));
const model = new LocalQwen();
await model.preflight();
const output = await enrich(snapshot, model);
await fs.writeFile(
  outputPath,
  JSON.stringify(
    {
      version: VERSION,
      model: model.identity,
      input_hash: hash(snapshot),
      ...output,
    },
    null,
    2,
  ),
  { flag: "wx" },
);
console.log(
  JSON.stringify({
    topics: output.topics.length,
    relations: output.relations.length,
    saved_to_db: false,
  }),
);
