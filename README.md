<div align="center">

<img src="https://raw.githubusercontent.com/daggerstuff/foresight/main/docs/assets/foresight-logo-dark.svg" alt="Foresight" width="220" />

# Foresight

**Memory that persists across the void between sessions.**

[![PyPI](https://img.shields.io/pypi/v/foresight?style=flat-square&color=1a1a2e&labelColor=0f0f1a&logo=pypi&logoColor=white)](https://pypi.org/project/foresight/)
[![Python](https://img.shields.io/badge/Python-3.12+-1a1a2e?style=flat-square&labelColor=0f0f1a&logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-1a1a2e?style=flat-square&labelColor=0f0f1a)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/daggerstuff/foresight/ci.yml?style=flat-square&labelColor=0f0f1a)](https://github.com/daggerstuff/foresight/actions)

</div>

---

Every conversation ends. The context window closes. What was learned, decided,
felt — gone.

Foresight gives agents a place to put it. Not a log file. Not a prompt trick. A
memory system with semantic search, temporal decay, emotional context, and
relationship graphs. Memories that know how they relate to each other. Context
that surfaces when it matters and fades when it doesn't.

It runs as an MCP server, a CLI, a TUI, or a Python SDK. Postgres holds the
truth. Vectors handle the recall. The agent decides what's worth remembering.

<div align="center">

### Get started

```bash
pip install foresight[all]
export FORESIGHT_DB_URL='postgresql://user:pass@host:5432/db?sslmode=require'
foresight init && foresight doctor && foresight tui
```

Or let the installer handle everything:

```bash
bash install.sh
```

</div>

---

<div align="center">

| Surface            | What it is                                                   |
| :----------------- | :----------------------------------------------------------- |
| `foresight` CLI    | 20+ commands for storing, searching, and managing memories   |
| `foresight tui`    | Interactive terminal interface — keyboard-first, full-screen |
| `foresight-server` | MCP server for Claude Code, Cursor, Goose, any MCP client    |
| Python SDK         | Import directly for custom tooling and scripts               |

</div>

---

<div align="center">

_The best agents remember. The rest repeat their mistakes._

</div>
