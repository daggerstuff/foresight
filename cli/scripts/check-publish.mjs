#!/usr/bin/env node
// can-i-publish gate — runs before `pnpm link` / publish.
// Verifies the package name is publishable and warns about the `foresight` bin
// colliding with the existing Python `foresight` executable on PATH.
import { execSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const pkg = JSON.parse(readFileSync(resolve(here, "..", "package.json"), "utf8"));

const name = pkg.name;
const bin = Object.keys(pkg.bin ?? {})[0] ?? "foresight";

let blocked = false;
const warnings = [];

// 1. Package name availability on npm.
try {
  const published = execSync(`npm view ${name} version`, {
    stdio: ["ignore", "pipe", "ignore"],
  })
    .toString()
    .trim();
  if (published) {
    warnings.push(
      `⚠ package name "${name}" is already published on npm (v${published}). ` +
        `Publishing will require a different name or ownership transfer.`,
    );
  }
} catch {
  // name not found on npm → good, publishable.
}

// 2. Local bin collision with the Python `foresight` executable.
try {
  const resolved = execSync(`command -v ${bin}`, { stdio: ["ignore", "pipe", "ignore"] })
    .toString()
    .trim();
  if (resolved) {
    warnings.push(
      `⚠ bin "${bin}" already resolves on PATH → ${resolved}. ` +
        `Linking this CLI will shadow or conflict with that executable. ` +
        `Use an isolated prefix (e.g. \`pnpm link --global\` in a dedicated env) ` +
        `or rename the bin to avoid clobbering the Python \`foresight\` tool.`,
    );
  }
} catch {
  // not on PATH → fine
}

if (warnings.length) {
  console.warn("\nforesight-cli · publish gate\n");
  for (const w of warnings) console.warn(w + "\n");
  console.warn("Proceeding with link anyway. Review the warnings above.\n");
}

if (blocked) {
  console.error("publish gate failed — aborting.");
  process.exit(1);
}

console.log(`publish gate OK · name=${name} bin=${bin}`);
