import ora, { type Ora } from "ora";
import pc from "picocolors";
import * as clack from "@clack/prompts";

export { pc, clack };

export function spinner(text: string): Ora {
  return ora({ text, spinner: "dots" }).start();
}

export function ok(text: string): void {
  console.log(pc.green("✔ ") + text);
}

export function fail(text: string): void {
  console.error(pc.red("✘ ") + text);
}

export function info(text: string): void {
  console.log(pc.cyan("ℹ ") + text);
}

export async function confirmPrompt(message: string): Promise<boolean> {
  const result = await clack.confirm({ message });
  return result === true;
}
