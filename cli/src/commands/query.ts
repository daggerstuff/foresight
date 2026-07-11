import { Command, Option } from "commander";
import { loadConfig } from "../lib/config.js";
import { withClient } from "../lib/db.js";
import { fail, info, spinner } from "../lib/ui.js";
import { formatMemory } from "./format.js";

interface QueryOpts {
  limit: string;
  category?: string;
  includeSensitive: boolean;
  json: boolean;
  local?: boolean;
}

export function registerQuery(cmd: Command): void {
  cmd
    .command("query <text>")
    .description("keyword search across memory content and tags")
    .option("-l, --limit <n>", "max rows", "10")
    .option("--category <cat>", "filter by category")
    .option("--include-sensitive", "include sensitive memories", false)
    .option("--json", "emit raw JSON array", false)
    .addOption(new Option("--local", "internal: local-only mode").hideHelp())
    .action(async (text: string, opts: QueryOpts) => {
      const cfg = loadConfig();
      const limit = Number.parseInt(opts.limit, 10) || 10;
      const s = spinner("searching");
      try {
        const rows = await withClient(cfg, async (client) => {
          const where: string[] = [
            "tenant_id = $1",
            "user_id = $2",
            "(content ILIKE $3 OR tags::text ILIKE $3)",
          ];
          const params: unknown[] = [cfg.account, cfg.userId, `%${text}%`];
          if (!opts.includeSensitive) where.push("is_sensitive = 0");
          if (opts.category) {
            params.push(opts.category);
            where.push(`category = $${params.length}`);
          }
          const res = await client.query(
            `SELECT id, content, scope, category, retention, created_at, tags, is_sensitive
               FROM memories
              WHERE ${where.join(" AND ")}
              ORDER BY created_at DESC
              LIMIT $${params.length + 1}`,
            [...params, limit],
          );
          return res.rows;
        });
        s.stop();
        if (rows.length === 0) {
          info("no matches");
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
