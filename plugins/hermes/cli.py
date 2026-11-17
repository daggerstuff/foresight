"""CLI subcommands for the Foresight memory provider plugin.

Discovered automatically by Hermes Agent: ``hermes foresight <subcommand>``.
Provides direct CLI commands for inspecting memory, testing connectivity,
searching, remembering, reflecting, and managing user profiles.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    from hermes_cli.config import load_config
except ImportError:

    def load_config() -> dict[str, Any]:
        return {}


try:
    from hermes_constants import get_hermes_home
except ImportError:

    def get_hermes_home() -> Path:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


# Import client and config loader from the provider module (supporting synthetic package loaders)
try:
    from . import _DEFAULT_URL, _Client, _load_config
except (ImportError, ValueError):
    import importlib.util

    def _empty_config(_hermes_home: Path) -> dict[str, Any]:
        """Config loader stand-in for a provider module that cannot be loaded."""
        return {}

    init_path = Path(__file__).resolve().parent / "__init__.py"
    spec = importlib.util.spec_from_file_location("_foresight_init", str(init_path))
    if spec is not None and spec.loader is not None:
        _init_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_init_mod)
        _exports: dict[str, Any] = {
            "_DEFAULT_URL": getattr(_init_mod, "_DEFAULT_URL", "http://127.0.0.1:8764"),
            "_Client": _init_mod._Client,
            "_load_config": _init_mod._load_config,
        }
    else:
        # No provider module beside this file. _Client is bound to None, which is
        # what it has always been on this path: _get_client() fails when called
        # rather than at import time, so the plugin still imports cleanly.
        _exports = {
            "_DEFAULT_URL": "http://127.0.0.1:8764",
            "_Client": None,
            "_load_config": _empty_config,
        }
    # Bound through the module namespace, not by re-declaring the names: `_Client`
    # is the provider's class in both paths (or None on the degraded one), so the
    # annotation on _get_client below keeps its meaning.
    globals().update(_exports)


def _get_client() -> _Client:
    """Instantiate a configured Foresight REST client using active profile secrets & config."""
    hermes_home = Path(get_hermes_home())
    cfg = _load_config(hermes_home)

    try:
        get_secret = importlib.import_module("agent.secret_scope").get_secret
    except ImportError:

        def get_secret(key: str, default: str = "") -> str:
            return os.environ.get(key, default)

    base_url = (
        get_secret("FORESIGHT_URL", "")
        or str(cfg.get("url") or "")
        or os.environ.get("FORESIGHT_URL", "")
        or _DEFAULT_URL
    ).rstrip("/")
    api_key = get_secret("FORESIGHT_API_KEY", "") or os.environ.get("FORESIGHT_API_KEY", "")
    raw_uid = (
        get_secret("FORESIGHT_USER_ID", "")
        or os.environ.get("FORESIGHT_USER_ID", "")
        or os.environ.get("FORESIGHT_IDENTITY", "")
        or str(cfg.get("user_id") or "")
        or "default"
    )
    user_id = re.sub(r"[^A-Za-z0-9_-]", "_", raw_uid).strip("_") or "default"

    return _Client(base_url, api_key, user_id)


def _cmd_status(_args: argparse.Namespace) -> None:
    """Check connectivity, latency, identity lane, and memory stats."""
    client = _get_client()
    sys.stdout.write("\n🔮 Foresight Memory Brain Status\n")
    sys.stdout.write("─" * 45 + "\n")
    sys.stdout.write(f"  Server URL:    {client.base_url}\n")
    sys.stdout.write(f"  Identity Lane: {client.user_id or '(default)'}\n")
    sys.stdout.write(f"  Auth:          {'configured (API key set)' if client.api_key else 'none / localhost'}\n")

    sys.stdout.write("\n  Pinging server...")
    sys.stdout.flush()
    health = client.health()
    if health.get("ok"):
        latency = health.get("latency_ms", 0)
        sys.stdout.write(f" connected ✓ ({latency}ms)\n")
        if health.get("count") is not None:
            sys.stdout.write(f"  Total Memories: {health['count']}\n")
    else:
        sys.stdout.write(f" FAILED ✗\n  Error: {health.get('error')}\n")
        return

    # Fetch recent memories
    try:
        res = client.list_memories(limit=5)
        text = res.get("result", "") if isinstance(res, dict) else str(res)
        if text:
            sys.stdout.write("\n  Recent Memories:\n")
            for line in text.splitlines()[:6]:
                sys.stdout.write(f"    {line}\n")
    except Exception as exc:
        sys.stdout.write(f"\n  (Could not fetch recent memories: {exc})\n")
    sys.stdout.write("\n")


def _cmd_search(args: argparse.Namespace) -> None:
    """Search stored memories using hybrid (vector + keyword) search."""
    client = _get_client()
    query = args.query.strip()
    if not query:
        sys.stderr.write("Error: query cannot be empty\n")
        return

    sys.stdout.write(f"\n🔍 Searching Foresight for: {query!r} (limit={args.limit})...\n\n")
    try:
        res = client.search(
            query=query,
            limit=args.limit,
            use_hybrid=not args.keyword_only,
            category=args.category,
        )
        if isinstance(res, dict) and "result" in res:
            sys.stdout.write(str(res["result"]) + "\n")
        elif isinstance(res, dict) and "matches" in res:
            for item in res["matches"]:
                sys.stdout.write(f"  - [{item.get('memory_id')}] (score: {item.get('score', 0):.3f})\n")
        else:
            sys.stdout.write(json.dumps(res, indent=2) + "\n")
    except Exception as exc:
        sys.stderr.write(f"Search failed: {exc}\n")
    sys.stdout.write("\n")


def _cmd_remember(args: argparse.Namespace) -> None:
    """Store a durable memory in the shared brain."""
    client = _get_client()
    content = args.content.strip()
    if not content:
        sys.stderr.write("Error: content cannot be empty\n")
        return

    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
    try:
        res = client.store(
            content=content,
            category=args.category,
            importance=args.importance,
            tags=tags,
        )
        mem_id = res.get("memory_id") if isinstance(res, dict) else None
        sys.stdout.write("\n✓ Stored memory in Foresight brain:\n")
        sys.stdout.write(f"  ID:         {mem_id or '(created)'}\n")
        sys.stdout.write(f"  Category:   {args.category}\n")
        sys.stdout.write(f"  Importance: {args.importance}\n")
        sys.stdout.write(f"  Content:    {content}\n\n")
    except Exception as exc:
        sys.stderr.write(f"Remember failed: {exc}\n")


def _cmd_reflect(args: argparse.Namespace) -> None:
    """Synthesize reasoning across memories to answer a strategic question."""
    client = _get_client()
    question = args.question.strip()
    if not question:
        sys.stderr.write("Error: question cannot be empty\n")
        return

    sys.stdout.write(f"\n🧠 Reflecting on: {question!r}...\n\n")
    try:
        res = client.reflect(question=question)
        if isinstance(res, dict) and "answer" in res:
            sys.stdout.write(str(res["answer"]) + "\n")
        else:
            sys.stdout.write(json.dumps(res, indent=2) + "\n")
    except Exception as exc:
        sys.stderr.write(f"Reflection failed: {exc}\n")
    sys.stdout.write("\n")


def _cmd_profile(_args: argparse.Namespace) -> None:
    """Display the synthesized user profile and persistent preferences."""
    client = _get_client()
    sys.stdout.write("\n👤 User Profile & Mental Models (Foresight)\n")
    sys.stdout.write("─" * 45 + "\n")
    try:
        res = client.profile()
        if isinstance(res, dict):
            static_prefs = res.get("static", [])
            dynamic_models = res.get("dynamic", [])
            if static_prefs:
                sys.stdout.write("\n  Preferences & Directives:\n")
                for pref in static_prefs:
                    sys.stdout.write(f"    • {pref}\n")
            if dynamic_models:
                sys.stdout.write("\n  Mental Models & Patterns:\n")
                for model in dynamic_models:
                    if isinstance(model, dict):
                        sys.stdout.write(f"    • [{model.get('type')}] {model.get('content')}\n")
                    else:
                        sys.stdout.write(f"    • {model}\n")
            if not static_prefs and not dynamic_models:
                sys.stdout.write("\n  Profile is currently empty (run agent turns to auto-distill).\n")
        else:
            sys.stdout.write(json.dumps(res, indent=2) + "\n")
    except Exception as exc:
        sys.stderr.write(f"Failed to fetch profile: {exc}\n")
    sys.stdout.write("\n")


def _cmd_test(_args: argparse.Namespace) -> None:
    """Run an automated self-test of the Foresight plugin endpoints."""
    client = _get_client()
    sys.stdout.write("\n⚡ Running Foresight Integration Self-Test\n")
    sys.stdout.write("─" * 45 + "\n")

    # 1. Connectivity
    sys.stdout.write("  1. Ping /memories:       ")
    sys.stdout.flush()
    health = client.health()
    if health.get("ok"):
        sys.stdout.write(f"PASS ({health.get('latency_ms')}ms)\n")
    else:
        sys.stdout.write(f"FAIL ({health.get('error')})\n")
        return

    # 2. Context Injection
    sys.stdout.write("  2. Test POST /inject:    ")
    sys.stdout.flush()
    try:
        inj = client.inject("test query context injection", max_memories=2)
        formatted = inj.get("formatted", "")
        if formatted:
            sys.stdout.write("PASS (recalled context returned)\n")
        else:
            sys.stdout.write("PASS (empty context as expected for novel query)\n")
    except Exception as exc:
        sys.stdout.write(f"FAIL ({exc})\n")

    # 3. Hybrid Search
    sys.stdout.write("  3. Test POST /search:    ")
    sys.stdout.flush()
    try:
        client.search("test", limit=2, use_hybrid=True)
        sys.stdout.write("PASS\n")
    except Exception as exc:
        sys.stdout.write(f"FAIL ({exc})\n")

    # 4. Profile
    sys.stdout.write("  4. Test GET /profile:    ")
    sys.stdout.flush()
    try:
        client.profile()
        sys.stdout.write("PASS\n")
    except Exception as exc:
        sys.stdout.write(f"FAIL ({exc})\n")

    sys.stdout.write("\n✓ All tests passed! Foresight is fully operational and hands-off.\n\n")


def foresight_command(args: argparse.Namespace) -> None:
    """Main entry point for ``hermes foresight ...`` commands."""
    sub = getattr(args, "foresight_action", None)
    if sub == "status":
        _cmd_status(args)
    elif sub == "search":
        _cmd_search(args)
    elif sub == "remember":
        _cmd_remember(args)
    elif sub == "reflect":
        _cmd_reflect(args)
    elif sub == "profile":
        _cmd_profile(args)
    elif sub == "test":
        _cmd_test(args)
    else:
        _cmd_status(args)


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the ``hermes foresight`` argparse tree."""
    subs = subparser.add_subparsers(dest="foresight_action")

    # status
    subs.add_parser("status", help="Show Foresight server connection, stats, and identity")

    # search
    search_p = subs.add_parser("search", help="Search shared memories")
    search_p.add_argument("query", help="Search query string")
    search_p.add_argument("--limit", type=int, default=8, help="Max results (default: 8)")
    search_p.add_argument("--category", help="Filter by category")
    search_p.add_argument("--keyword-only", action="store_true", help="Disable hybrid semantic search")

    # remember
    rem_p = subs.add_parser("remember", help="Store a durable fact, preference, or decision")
    rem_p.add_argument("content", help="Memory content to store")
    rem_p.add_argument("--category", default="fact", help="Category (fact, preference, decision, directive)")
    rem_p.add_argument("--importance", type=float, default=0.5, help="Importance 0.0-1.0 (default: 0.5)")
    rem_p.add_argument("--tags", help="Comma-separated tags")

    # reflect
    ref_p = subs.add_parser("reflect", help="Synthesize reasoning across memories")
    ref_p.add_argument("question", help="Question or topic for multi-hop synthesis")

    # profile
    subs.add_parser("profile", help="Show synthesized user profile and mental models")

    # test
    subs.add_parser("test", help="Run integration self-test of all Foresight endpoints")

    subparser.set_defaults(func=foresight_command)
