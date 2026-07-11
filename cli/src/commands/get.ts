import { Command } from "commander";
import { loadConfig } from "../lib/config.js";
import { withClient } from "../lib/db.js";
import { fail, info, spinner } from "../lib/ui.js";
import { formatMemory } from "./format.js";

interface GetOpts {
  json: boolean;
}

export function registerGet(cmd: Command): void {
  cmd
    .command("get <id>")
    .description("fetch a single memory by id")
    .option("--json", "emit raw JSON", false)
    .action(async (id: string, opts: GetOpts) => {
      const cfg = loadConfig();
      const s = spinner("fetching memory");
      try {
        const row = await withClient(cfg, async (client) => {
          const res = await client.query(
            `SELECT * FROM memories
              WHERE id = $1 AND tenant_id = $2 AND user_id = $3`,
            [id, cfg.account, cfg.userId],
          );
          return res.rows[0];
        });
        s.stop();
        if (!row) {
          fail(`memory ${id} not found`);
          process.exitCode = 1;
          return;
        }
        if (opts.json) console.log(JSON.stringify(row, null, 2));
        else console.log(formatMemory(row as Record<string, unknown>));
      } catch (e) {
        s.stop();
        fail(e instanceof Error ? e.message : String(e));
        process.exitCode = 1;
      }
    });
}
