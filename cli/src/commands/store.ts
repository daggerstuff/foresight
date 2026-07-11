import { Command, Option } from "commander";
import { randomUUID } from "node:crypto";
import { loadConfig } from "../lib/config.js";
import { withClient } from "../lib/db.js";
import { fail, ok, pc, spinner } from "../lib/ui.js";

interface StoreOpts {
  category: string;
  scope: string;
  retention: string;
  tags: string;
  bank?: string;
  sensitive: boolean;
  reason?: string;
  local?: boolean;
}

export function registerStore(cmd: Command): void {
  cmd
    .command("store <content>")
    .description("store a new memory")
    .option("-c, --category <cat>", "memory category", "fact")
    .option("-s, --scope <scope>", "memory scope", "session")
    .option("-r, --retention <retention>", "retention policy", "short_term")
    .option("-t, --tags <tags>", "comma-separated tags", "")
    .option("--bank <bank>", "bank id")
    .option("--sensitive", "mark memory as sensitive (HIPAA-gated)", false)
    .option("--reason <reason>", "reason for sensitivity marking")
    .addOption(new Option("--local", "internal: local-only mode").hideHelp())
    .action(async (content: string, opts: StoreOpts) => {
      const cfg = loadConfig();
      const tags = opts.tags
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean);
      const s = spinner("storing memory");
      try {
        const id = randomUUID();
        const now = new Date().toISOString();
        await withClient(cfg, async (client) => {
          await client.query(
            `INSERT INTO memories
              (id, content, tenant_id, scope, retention, category, user_id, bank_id,
               created_at, updated_at, tags, is_sensitive, is_ghost, version,
               sensitivity_reason)
             VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,0,1,$13)`,
            [
              id,
              content,
              cfg.account,
              opts.scope,
              opts.retention,
              opts.category,
              cfg.userId,
              opts.bank ?? cfg.bankId,
              now,
              now,
              JSON.stringify(tags),
              opts.sensitive ? 1 : 0,
              opts.sensitive ? (opts.reason ?? "flagged via CLI") : null,
            ],
          );
        });
        s.stop();
        ok(`stored ${pc.dim(id)}`);
      } catch (e) {
        s.stop();
        fail(e instanceof Error ? e.message : String(e));
        process.exitCode = 1;
      }
    });
}
