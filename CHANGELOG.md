# Changelog

All notable changes to Foresight MCP are documented here.

## [Unreleased]

### Added

- **Skill crystallization (`foresight.skill_crystallization`, FOR-55):**
  Procedural observations crossing the proof threshold (`N` successful episodes
  with zero unresolved contradicting failures, excluding `is_sensitive` rows)
  are rendered as `SKILL.md` artifacts, HMAC-SHA256 signed with the
  `profile_export.py` key, versioned in `memory_versions`, and staged in the
  `manage_inferred` review queue.
- **Observe-first staging & gated loading:** `manage_curation_runs` surfaces
  `skill_candidates` and stages `SKILL.md` artifacts (`inferred=1`); only
  human-approved, signature-verified skills are exposed to agents via
  `inject_context`, `load_skill`, `foresight://skills`, and REST `/skills`.
- **Evidence-driven retirement:** Skills auto-retire (and are flagged in
  `manage_inferred`) when underlying observations or supporting memories are
  superseded, contradicted, archived, deleted, or expired.
- **CLI & REST support:** Added `foresight skills` (`crystallize`, `list`,
  `approve`, `decline`, `export`, `verify`) writing `skills/<name>/SKILL.md`,
  plus `/skills`, `/skills/{skill_name}`, and `/skills/crystallize` REST routes.

## [0.19.0] - 2026-07-15

### Added

- `scripts/drain_sqlite_to_postgres.py`: drains any pre-existing standalone
  SQLite data (`~/.foresight/operations.db`,
  `~/.foresight/narrative_cache.sqlite`) into Postgres idempotently
  (`ON CONFLICT DO NOTHING`).
- `scripts/rollout_fleet.sh`: fleet rollout helper for the Postgres-only
  migration. Dry-run by default; pass `--apply` to drain legacy SQLite and
  restart `foresight-mcp` on local / billy / gnasty.

### Fixed

- `foresight export` no longer silently fails. It previously relied on
  `search_memories(query_type="list")`, which returns a formatted string rather
  than a list, so the `isinstance(result, list)` guard always raised and exited.
  Export now reads directly from PostgreSQL via `get_db_connection()`.

### Changed

- **Postgres-only storage.** Foresight has no local SQLite backend; the daemon
  fails fast if `FORESIGHT_DB_URL` is unset. Documentation updated throughout
  (README, example MCP client configs).
- `OperationQueue` (`foresight/sync.py`) and `NarrativeCache`
  (`foresight/narrative_cache.py`) now read/write the shared Postgres
  `operations` and `narrative_cache` tables created by `init_db` (schema v12).
  Standalone SQLite files are no longer created or written.
- **CLI config rename:** `db_path` → `db_url` and `FORESIGHT_DB_PATH` →
  `FORESIGHT_DB_URL` across the CLI. `FORESIGHT_DB_PATH` is still recognized and
  emits a deprecation warning (it is no longer used). The `eval` command's
  `--db-path` option is intentionally retained — it points at an isolated temp
  SQLite database for the evaluation harness, not the main store.
- Version 0.18.1 → 0.19.0.

### Notes

- The SQLite→Postgres migration is complete. Persisted memory now lives solely
  in the shared Ghost Postgres instance; no SQLite store remains.
