import { Command, Option } from "commander";
import { loadConfig } from "../lib/config.js";
import { withClient } from "../lib/db.js";
import { fail, info, spinner } from "../lib/ui.js";
import { formatMemory } from "./format.js";

interface ListOpts {
  limit: string;
  category?: string;
  includeSensitive: boolean;
  local?: boolean;
}

export function registerList(cmd: Command): void {
  cmd
    .command("list")
    .description("list memories for the current identity")
    .option("-l, --limit <n>", "max rows", "20")
    .option("--category <cat>", "filter by category")
    .option("--include-sensitive", "include sensitive memories", false)
    .addOption(new Option("--local", "internal: local-only mode").hideHelp())
    .action(async (_: unknown, opts: ListOpts) => {
      const cfg = loadConfig();
      const limit = Number.parseInt(opts.limit, 10) || 20;
      const s = spinner("loading memories");
      try {
        const rows = await withClient(cfg, async (client) => {
          const where: string[] = ["tenant_id = $1", "user_id = $2"];
          const params: unknown[] = [cfg.account, cfg.userId];
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
          info("no memories found");
          return;
        }
        for (const m of rows) console.log(formatMemory(m as Record<string, unknown>));
      } catch (e) {
        s.stop();
        fail(e instanceof Error ? e.message : String(e));
        process.exitCode = 1;
      }
    });
}
