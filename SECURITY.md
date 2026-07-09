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
   The rotated replacement is delivered out-of-band to the maintainers
   through the team's secret manager; it is **never** echoed in commit
   messages, pull-request bodies, plan files, security advisories, or chat
   transcripts.
3. **Sanitize working copy** — secret scan the local repository
   (`git grep -F <redacted>` plus a full `git cat-file --batch-all-objects
--batch` scan against the object store, reachable + unreachable) before
   crafting the history-rewrite branch.
4. **Rewrite pushed history** via `git filter-repo` (`--replace-text`) on a
   dedicated remediation branch. The branch is force-pushed and a pull
   request opened against `master` — `master` itself is never
   rewritten or force-pushed without explicit maintainer authorization.
5. **Verify** — every leaked-commit reference (`git merge-base
--is-ancestor`) returns NEGATIVE after the rewrite, the GitHub commit
   API returns the rewritten form, and the upstream secret-scan advisory
   (e.g. GitGuardian, GitHub secret-scanning) clears.
6. **Prevent recurrence** — add pre-commit secret scanning (e.g.
   `ggshield`) plus a GitHub Actions `secrets-scan.yml` workflow that runs
   on every push and pull request. Add a regression test that fails the
   build if any string matched by the scanner appears in the working tree.

## Scope

The following are within scope for coordinated disclosure:

- Credential disclosure in commit history (database DSNs, API tokens,
  private keys, signing keys, session cookies).
- Hardcoded credentials in source, fixtures, configuration templates, or
  documentation that ships in the published package.
- Vulnerabilities in installed hooks, MCP servers, or remote procedure
  endpoints that allow privilege escalation, data exfiltration, or
  unauthorized memory access.
- Dependency confusion, typosquatting, or supply-chain compromise of
  declared dependencies.

Out of scope:

- Issues already publicly disclosed.
- Theoretical denials of service that require compromising upstream
  infrastructure.
- Reports against forks of this repository.

## Acknowledgments

We thank the security research community — including Robin (Germany-based
GitHub secret-scanner) — for the report that motivated this policy and the
preventive controls now wired into the project's pre-commit and CI hooks.

## Past Incidents

| Report date | Researcher | Reference                                 | Resolution                                                                                                                                                                                        |
| ----------- | ---------- | ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 2026-07-08  | Robin      | `60be613c` (orphan, never reached origin) | Credential rotated at operator; local blob physically pruned via `git gc --prune=now`; prevention controls (ggshield pre-commit + CI) added. Credential string omitted from this table by policy. |

(Acknowledgments are made in good faith; identity is recorded only when the
researcher consents.)
