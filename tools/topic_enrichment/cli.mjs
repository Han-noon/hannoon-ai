import fs from "node:fs/promises";
import { refresh } from "./core.mjs";
import { LocalQwen, SupabaseStore } from "./adapters.mjs";
const args = process.argv.slice(2),
  command = args[0] || "preview";
if (!["preview", "once", "watch"].includes(command))
  throw Error("Usage: node cli.mjs preview|once|watch [--config path]");
const index = args.indexOf("--config");
const file =
  index >= 0 ? args[index + 1] : new URL("./config.json", import.meta.url);
const config = JSON.parse(await fs.readFile(file, "utf8"));
const store = new SupabaseStore({
  url: process.env.SUPABASE_URL,
  key: process.env.SUPABASE_SERVICE_ROLE_KEY,
});
const model = new LocalQwen({
  url: config.ollama_url,
  model: config.model,
  cache: new Map(),
});
const options = {
  store,
  model,
  topicIds: config.topic_ids,
  dryRun: command === "preview",
  force: args.includes("--force"),
};
const seconds = config.poll_seconds ?? 60;
if (!Number.isInteger(seconds) || seconds < 15)
  throw Error("poll_seconds must be >= 15");
let stopping = false;
process.on("SIGINT", () => {
  stopping = true;
});
process.on("SIGTERM", () => {
  stopping = true;
});
do {
  try {
    console.log(JSON.stringify(await refresh(options), null, 2));
  } catch (error) {
    model.cache?.clear();
    console.error(error.message);
    if (command !== "watch") {
      process.exitCode = 1;
      break;
    }
  }
  if (command !== "watch" || stopping) break;
  await new Promise((resolve) => {
    const stop = () => {
      clearTimeout(timer);
      process.off("SIGINT", stop);
      process.off("SIGTERM", stop);
      resolve();
    };
    const timer = setTimeout(stop, seconds * 1000);
    process.once("SIGINT", stop);
    process.once("SIGTERM", stop);
  });
} while (!stopping);
