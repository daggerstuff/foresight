import { Command, Option } from "commander";
import { loadConfig } from "../lib/config.js";
import { withClient } from "../lib/db.js";
import { fail, info, spinner } from "../lib/ui.js";
import { formatMemory } from "./format.js";

interface InjectOpts {
  text: string;
  max: string;
  json: boolean;
  local?: boolean;
}

export function registerInjectContext(cmd: Command): void {
  cmd
    .command("inject-context")
    .description("surface memories relevant to a snippet of conversation text")
    .requiredOption("-t, --text <text>", "conversation text to match")
    .option("-m, --max <n>", "max memories", "5")
    .option("--json", "emit raw JSON array", false)
    .addOption(new Option("--local", "internal: local-only mode").hideHelp())
    .action(async (opts: InjectOpts) => {
      const cfg = loadConfig();
      const max = Number.parseInt(opts.max, 10) || 5;
      const s = spinner("matching context");
      try {
        const rows = await withClient(cfg, async (client) => {
          const res = await client.query(
            `SELECT id, content, scope, category, retention, created_at, tags, is_sensitive
               FROM memories
              WHERE tenant_id = $1 AND user_id = $2
                AND is_sensitive = 0
                AND (content ILIKE $3 OR tags::text ILIKE $3)
              ORDER BY created_at DESC
              LIMIT $4`,
            [cfg.account, cfg.userId, `%${opts.text}%`, max],
          );
          return res.rows;
        });
        s.stop();
        if (rows.length === 0) {
          info("no relevant context");
          return;
        }
        if (opts.json) console.log(JSON.stringify(rows, null, 2));
        else for (const m of rows) console.log(formatMemory(m as Record<string, unknown>));
      } catch (e) {
        s.stop();
        fail(e instanceof Error ? e.message : String(e));
        process.exitCode = 1;
      }
    });
}
