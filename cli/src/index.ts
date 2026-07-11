import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { Command } from "commander";
import pc from "picocolors";
import { renderBanner } from "./banner.js";
import { registerAll } from "./commands/index.js";
import { closeConnections } from "./lib/db.js";

const here = dirname(fileURLToPath(import.meta.url));
const pkg = JSON.parse(readFileSync(join(here, "../package.json"), "utf8")) as {
  version: string;
};
const VERSION = pkg.version;

function shouldShowBanner(argv: string[]): boolean {
  if (!process.stdout.isTTY) return false;
  const quiet = argv.some((a) => ["-v", "--version", "-h", "--help"].includes(a));
  return !quiet;
}

async function main(): Promise<void> {
  const program = new Command();
  program
    .name("foresight")
    .description("foresight memory CLI — surface, store, and manage durable context")
    .version(VERSION, "-v, --version", "output the version number")
    .helpOption("-h, --help", "display help");

  registerAll(program);

  if (shouldShowBanner(process.argv)) {
    console.log(renderBanner({ version: VERSION }));
  }

  await program.parseAsync(process.argv);
  await closeConnections();
}

main().catch((e) => {
  console.error(pc.red(e instanceof Error ? e.message : String(e)));
  process.exitCode = 1;
});
