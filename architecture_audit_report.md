**Actions taken**
- Ran a batch MCP command to index the repository and extracted architectural signals (module layout, imports, config, data models, layering, file size, DI usage).
- Analyzed results to identify structural patterns and pain points.

**Key findings**
1. **Monolithic package layout** – All backend code lives in a single `foresight_mcp` package with ~40 modules, causing tight coupling and potential circular imports.
2. **Missing layered separation** – No clear domain/repository/service/API layers; modules mix persistence, business logic, and transport handling.
3. **Scattered configuration** – Settings are hard‑coded across modules; lacks a centralized, environment‑driven configuration system.
4. **Limited data modeling** – Raw `dataclass` definitions are used without validation libraries (e.g., Pydantic), leading to duplicated validation logic.
5. **Large multi‑purpose modules** – Files like `auth.py` and `server.py` exceed 500 LOC and combine several responsibilities.
6. **No dependency injection** – Direct imports of connection pools and config values create tight coupling and hinder testing.
7. **Front‑end / TypeScript core decoupled** – No shared API contract (OpenAPI) between Python backend and `packages/foresight-core` TypeScript library, risking drift.
8. **Documentation not integrated** – Docs exist but are not auto‑generated from code, risking divergence.

**Suggested refactorings**
- Introduce layered architecture: `models/`, `db/`, `services/`, `api/`.
- Adopt a DI framework (e.g., `dependency-injector`).
- Centralize configuration with a Pydantic `Settings` class.
- Split large modules into focused sub‑modules.
- Generate OpenAPI spec and TypeScript client types.
- Integrate automated doc generation (mkdocstrings/sphinx‑autodoc) with Docusaurus.
- Run static analysis for circular imports after refactoring.
