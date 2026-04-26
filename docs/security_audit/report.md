# Security Audit Report – Pixelated Empathy

## Findings

1. **No unsafe code execution** – No use of `eval`, `exec`, `os.system`, or `subprocess` observed.
2. **Secure secret generation** – API keys and passwords are generated with `secrets.token_urlsafe` and stored using PBKDF2‑SHA256.
3. **Parameterized SQL** – All database interactions use SQLite parameter placeholders (`?`) and whitelist validation (`sql_helpers`).
4. **Insecure default: optional API‑key enforcement** – `FORESIGHT_REQUIRE_API_KEY` defaults to `false`, allowing unauthenticated API access if the middleware is misconfigured.
5. **Default admin creation** – `initialize_default_users()` auto‑creates an admin/readonly user when the user table is empty. In production this could be abused if the generated password is logged or exposed.
6. **Password hashing** – Uses PBKDF2 with SHA256, which is acceptable but the comment notes a “simplified bcrypt‑like” implementation; a dedicated library (bcrypt/argon2) would provide stronger hardening and automatic salt handling.
7. **Configuration defaults** – Database path and identifiers default to user‑writable locations; ensure file permissions restrict unauthorized access.

## Recommendations

- **Enforce API‑key by default**: Change `FORESIGHT_REQUIRE_API_KEY` default to `true` or require explicit opt‑out via a secure flag.
- **Disable auto‑creation of admin accounts in production**: Guard `initialize_default_users()` with an environment variable (e.g., `FORESIGHT_ALLOW_DEFAULT_USERS`) and log generated credentials securely.
- **Upgrade password hashing**: Replace custom PBKDF2 implementation with `argon2-cffi` or `bcrypt` for stronger resistance to GPU attacks.
- **Audit secret handling**: Ensure generated passwords are never written to logs; consider using a secret vault for initial admin credentials.
- **Review file permissions**: Restrict `~/.foresight` directory to owner‑only (`chmod 700`).
- **Add rate‑limit enforcement**: Implement middleware to enforce `DEFAULT_RATE_LIMIT` and burst limits per IP.
- **Static analysis**: Integrate a SAST tool (e.g., Bandit, Semgrep) into CI to detect future regressions.

## Next Steps

- Add configuration flags for the above mitigations.
- Introduce a CI job that runs Bandit and fails on high‑severity findings.
- Document the security hardening process in the project README.
