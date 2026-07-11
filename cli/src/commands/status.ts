import { Command, Option } from "commander";
import { loadConfig } from "../lib/config.js";
import { getPool, getRedis, closeConnections } from "../lib/db.js";
import { fail, info, spinner } from "../lib/ui.js";

interface StatusOpts {
  local?: boolean;
}

export function registerStatus(cmd: Command): void {
  cmd
    .command("status")
    .description("system status — memory counts, cache, recent activity")
    .addOption(new Option("--local", "internal: local-only mode").hideHelp())
    .action(async (_: unknown, _opts: StatusOpts) => {
      const cfg = loadConfig();
      const s = spinner("gathering status");
      try {
        const pool = getPool(cfg);
        const counts = await pool.query(
          `SELECT
             COUNT(*) FILTER (WHERE tenant_id = $1 AND user_id = $2) AS total,
             COUNT(*) FILTER (WHERE tenant_id = $1 AND user_id = $2 AND is_sensitive = 1) AS sensitive,
             COUNT(*) FILTER (WHERE tenant_id = $1 AND user_id = $2 AND created_at >= NOW() - INTERVAL '30 days') AS last30
           FROM memories`,
          [cfg.account, cfg.userId],
        );
        const c = counts.rows[0];

        let cache = "n/a";
        if (cfg.redisUrl) {
          const redis = getRedis(cfg);
          const size = await redis.dbsize();
          cache = `${size} keys`;
        }
        s.stop();

        info(`identity      : ${cfg.userId}@${cfg.account}`);
        info(`memories total : ${c.total}`);
        info(`sensitive      : ${c.sensitive}`);
        info(`created 30d    : ${c.last30}`);
        info(`cache (redis)  : ${cache}`);
      } catch (e) {
        s.stop();
        fail(e instanceof Error ? e.message : String(e));
        process.exitCode = 1;
      } finally {
        await closeConnections();
      }
    });
}
