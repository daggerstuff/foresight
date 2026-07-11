import { Command, Option } from "commander";
import { loadConfig } from "../lib/config.js";
import { getPool, getRedis, closeConnections } from "../lib/db.js";
import { fail, info, ok, spinner } from "../lib/ui.js";

interface DoctorOpts {
  local?: boolean;
}

export function registerDoctor(cmd: Command): void {
  cmd
    .command("doctor")
    .description("health check — verifies database and cache connectivity")
    .addOption(new Option("--local", "internal: local-only mode").hideHelp())
    .action(async (_: unknown, _opts: DoctorOpts) => {
      const cfg = loadConfig();
      const s = spinner("running diagnostics");
      try {
        const pool = getPool(cfg);
        const pgOk = await pool.query("SELECT 1 AS ok");
        const pgHealth = pgOk.rows[0]?.ok === 1;

        let redisOk = false;
        let redisNote = "skipped (no FORESIGHT_REDIS_URL)";
        if (cfg.redisUrl) {
          const redis = getRedis(cfg);
          await redis.ping();
          redisOk = true;
          redisNote = "reachable";
        }
        s.stop();

        info(`postgres : ${pgHealth ? "ok" : "FAILED"}`);
        info(`redis    : ${redisOk ? "ok" : redisNote}`);
        info(`identity : ${cfg.userId}@${cfg.account}`);
        if (pgHealth) ok("foresight is healthy");
        else {
          fail("postgres connection failed");
          process.exitCode = 1;
        }
      } catch (e) {
        s.stop();
        fail(e instanceof Error ? e.message : String(e));
        process.exitCode = 1;
      } finally {
        await closeConnections();
      }
    });
}
