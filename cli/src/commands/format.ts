import pc from "picocolors";

export function parseTags(raw: string | null | undefined): string[] {
  if (!raw) return [];
  try {
    const v: unknown = JSON.parse(raw);
    if (!Array.isArray(v)) return [];
    return (v as unknown[]).map((x: unknown): string => {
      if (typeof x === "string") return x;
      if (typeof x === "number" || typeof x === "boolean") return String(x);
      return "";
    });
  } catch {
    return [];
  }
}

export function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

export function maskUrl(url?: string): string {
  if (!url) return pc.dim("(unset)");
  try {
    return new URL(url).host;
  } catch {
    return pc.dim("(invalid)");
  }
}

export function formatMemory(m: Record<string, unknown>): string {
  const id = typeof m.id === "string" || typeof m.id === "number" ? String(m.id) : "?";
  const cat =
    typeof m.category === "string" || typeof m.category === "number" ? String(m.category) : "?";
  const scope = typeof m.scope === "string" || typeof m.scope === "number" ? String(m.scope) : "";
  const sens = m.is_sensitive ? pc.red(" sensitive") : "";
  const tags = parseTags(m.tags as string | null);
  const head = `${pc.cyan(id.slice(0, 8))} ${pc.dim(`[${cat}] ${scope}`)}${sens}`;
  const body = `  ${truncate(typeof m.content === "string" ? m.content : "", 140)}`;
  const tagline = tags.length ? pc.gray(`  #${tags.join(" #")}`) : "";
  return [head, body, tagline].filter(Boolean).join("\n");
}
