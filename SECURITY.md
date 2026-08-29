# Security Policy

The Foresight team takes security seriously — this document describes how to
report vulnerabilities and how we coordinate disclosure.

## Reporting a Vulnerability

Please email **security@vectorize.io** with:

- A clear description of the issue and its impact.
- Step-by-step reproduction instructions, including any sample request payloads.
- The commit SHA, file path, and line number where the issue lives.

For sensitive reports (credential disclosures, supply-chain compromise, etc.)
prefer encrypted email — PGP keys are listed on the vectorize.io security page.
**Do not** open a public GitHub issue until coordinated disclosure completes.

## Coordinated Disclosure

When an external researcher reports a credential leak or other sensitive finding
against this repository we follow the timeline below:

1. **Acknowledge** — initial response within 72 hours, including an incident
   ticket reference.
2. **Rotate** — any leaked credential is invalidated at the upstream operator
   (database password, API token, signing key, etc.) before any further work.
   The rotated replacement is delivered out-of-band to the maintainers through
   the team's secret manager; it is **never** echoed in commit messages,
   pull-request bodies, plan files, security advisories, or chat transcripts.
3. **Sanitize working copy** — secret scan the local repository
   (`git grep -F <redacted>` plus a full
   `git cat-file --batch-all-objects --batch` scan against the object store,
   reachable + unreachable) before crafting the history-rewrite branch.
4. **Rewrite pushed history** via `git filter-repo` (`--replace-text`) on a
   dedicated remediation branch. The branch is force-pushed and a pull request
   opened against `master` — `master` itself is never rewritten or force-pushed
   without explicit maintainer authorization.
5. **Verify** — every leaked-commit reference (`git merge-base --is-ancestor`)
   returns NEGATIVE after the rewrite and the GitHub commit API returns the
   rewritten form.
6. **Prevent recurrence** — add pre-commit secret scanning and a CI workflow
   that runs on every push and pull request. Add a regression test that fails
   the build if any string matched by the scanner appears in the working tree.

## Scope

The following are within scope for coordinated disclosure:

- Credential disclosure in commit history (database DSNs, API tokens, private
  keys, signing keys, session cookies).
- Hardcoded credentials in source, fixtures, configuration templates, or
  documentation that ships in the published package.
- Vulnerabilities in installed hooks, MCP servers, or remote procedure endpoints
  that allow privilege escalation, data exfiltration, or unauthorized memory
  access.
- Dependency confusion, typosquatting, or supply-chain compromise of declared
  dependencies.

Out of scope:

- Issues already publicly disclosed.
- Theoretical denials of service that require compromising upstream
  infrastructure.
- Reports against forks of this repository.

## Threat Model

### Assets

| Asset | Classification | Notes |
| ----- | -------------- | ----- |
| Memory contents | Sensitive | May include personal, clinical, or proprietary context volunteered by users |
| Context blocks | Sensitive | Distilled user preferences, project state, patterns — cross-session by design |
| Tenant / user identifiers | Internal | Every store and query is scoped by `tenant_id` + `user_id` |
| `FORESIGHT_ENCRYPTION_KEY` | Secret | AES-256-GCM master key; optional at-rest encryption layer |
| `FORESIGHT_DB_URL` | Secret | Database DSN (Postgres or SQLite path) |
| LLM provider API keys | Secret | Outbound-only; used for extraction and semantic search backends |

### Trust Boundaries

```
Agent / CLI client ──── MCP protocol (stdio | HTTP) ────▶ Foresight MCP server
                                                            │
                              ┌─────────────────────────────┼──────────────────────────┐
                              ▼                             ▼                          ▼
                    Postgres / SQLite                LLM provider APIs            Redis / Kafka
                    (memory store)                   (outbound, extraction)       (companion streams)
```

1. **Client ↔ MCP server** — the server executes with the filesystem and network
   rights of its host process. Untrusted input arrives exclusively through tool
   arguments, which are schema-validated (pydantic) before use.
2. **Server ↔ memory store** — all queries are tenant-scoped; connection
   credentials come from the environment, never source.
3. **Server ↔ LLM providers** — outbound HTTP with user memory content in
   prompts. Providers are configured by the operator; content is sent only to
   endpoints the operator chose.
4. **Hooks** — pre/post memory hooks run operator-installed code with the
   server's privileges. Only hooks the operator installs are executed.

### STRIDE Summary

| Threat | Boundary | Mitigation |
| ------ | -------- | ---------- |
| Spoofing | Client ↔ server | MCP session/auth handled by transport; operator binds the listener |
| Tampering | Memory store | Optional AES-256-GCM encryption at rest; integrity via primary keys + versioning |
| Repudiation | Memory mutations | `memory_versions` table retains per-change history |
| Information disclosure | Cross-tenant | Every read/write scoped by `tenant_id` + `user_id`; regression tests assert scope isolation (`tests/test_memory_scope.py`) |
| Information disclosure | At rest | `manage_encryption encrypt_all` + `FORESIGHT_ENCRYPTION_KEY`; key rotation supported |
| Denial of service | Retrieval path | TF-IDF / hybrid caches with size caps; connection pooling with bounded pools |
| Elevation of privilege | Hooks | No bundled remote code execution; hooks are local operator-installed files |

### Secrets Handling

- Secrets are supplied **only** through environment variables
  (`FORESIGHT_DB_URL`, `FORESIGHT_ENCRYPTION_KEY`, provider keys). They are
  never hardcoded, logged, or echoed into tool output.
- CI runs **Trivy secret scanning** and **bandit** on every push and pull
  request; findings fail the build.
- Credential incidents follow the coordinated-disclosure process above,
  including history rewrite and operator-side rotation before any further work.

The full data-flow diagrams, STRIDE analysis, and risk register live in
[THREAT_MODEL.md](THREAT_MODEL.md).

## Acknowledgments

We thank the security research community — including Robin (Germany-based GitHub
secret-scanner) — for the report that motivated this policy and the preventive
controls now wired into the project's pre-commit and CI hooks.

## Past Incidents

| Report date | Researcher | Reference                                 | Resolution                                                                                                                                                             |
| ----------- | ---------- | ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 2026-07-08  | Robin      | `60be613c` (orphan, never reached origin) | Credential rotated at operator; local blob physically pruned via `git gc --prune=now`; prevention controls added. Credential string omitted from this table by policy. |

(Acknowledgments are made in good faith; identity is recorded only when the
researcher consents.)
