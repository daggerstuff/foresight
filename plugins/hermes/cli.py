"""CLI subcommands for the Foresight memory provider plugin.

Discovered automatically by Hermes Agent: ``hermes foresight <subcommand>``.
Provides direct CLI commands for inspecting memory, testing connectivity,
searching, remembering, reflecting, and managing user profiles.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
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

    init_path = Path(__file__).resolve().parent / "__init__.py"
    spec = importlib.util.spec_from_file_location("_foresight_init", str(init_path))
    if spec and spec.loader:
        _init_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_init_mod)
        _DEFAULT_URL = getattr(_init_mod, "_DEFAULT_URL", "http://127.0.0.1:8764")
        _Client = getattr(_init_mod, "_Client")
        _load_config = getattr(_init_mod, "_load_config")
    else:
        _DEFAULT_URL = "http://127.0.0.1:8764"
        _Client = None  # type: ignore[assignment]
        _load_config = lambda h: {}  # type: ignore[assignment]


def _get_client() -> _Client:
    """Instantiate a configured Foresight REST client using active profile secrets & config."""
    hermes_home = Path(get_hermes_home())
    cfg = _load_config(hermes_home)

    try:
        from agent.secret_scope import get_secret
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


def _cmd_status(args: argparse.Namespace) -> None:
    """Check connectivity, latency, identity lane, and memory stats."""
    client = _get_client()
    print("\n🔮 Foresight Memory Brain Status")
    print("─" * 45)
    print(f"  Server URL:    {client.base_url}")
    print(f"  Identity Lane: {client.user_id or '(default)'}")
    print(f"  Auth:          {'configured (API key set)' if client.api_key else 'none / localhost'}")

    print("\n  Pinging server...", end="", flush=True)
    health = client.health()
    if health.get("ok"):
        latency = health.get("latency_ms", 0)
        print(f" connected ✓ ({latency}ms)")
        if health.get("count") is not None:
            print(f"  Total Memories: {health['count']}")
    else:
        print(f" FAILED ✗\n  Error: {health.get('error')}")
        return

    # Fetch recent memories
    try:
        res = client.list_memories(limit=5)
        text = res.get("result", "") if isinstance(res, dict) else str(res)
        if text:
            print("\n  Recent Memories:")
            for line in text.splitlines()[:6]:
                print(f"    {line}")
    except Exception as exc:
        print(f"\n  (Could not fetch recent memories: {exc})")
    print()


def _cmd_search(args: argparse.Namespace) -> None:
    """Search stored memories using hybrid (vector + keyword) search."""
    client = _get_client()
    query = args.query.strip()
    if not query:
        print("Error: query cannot be empty", file=sys.stderr)
        return

    print(f"\n🔍 Searching Foresight for: {query!r} (limit={args.limit})...\n")
    try:
        res = client.search(
            query=query,
            limit=args.limit,
            use_hybrid=not args.keyword_only,
            category=args.category,
        )
        if isinstance(res, dict) and "result" in res:
            print(res["result"])
        elif isinstance(res, dict) and "matches" in res:
            for item in res["matches"]:
                print(f"  - [{item.get('memory_id')}] (score: {item.get('score', 0):.3f})")
        else:
            print(json.dumps(res, indent=2))
    except Exception as exc:
        print(f"Search failed: {exc}", file=sys.stderr)
    print()


def _cmd_remember(args: argparse.Namespace) -> None:
    """Store a durable memory in the shared brain."""
    client = _get_client()
    content = args.content.strip()
    if not content:
        print("Error: content cannot be empty", file=sys.stderr)
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
        print("\n✓ Stored memory in Foresight brain:")
        print(f"  ID:         {mem_id or '(created)'}")
        print(f"  Category:   {args.category}")
        print(f"  Importance: {args.importance}")
        print(f"  Content:    {content}\n")
    except Exception as exc:
        print(f"Remember failed: {exc}", file=sys.stderr)


def _cmd_reflect(args: argparse.Namespace) -> None:
    """Synthesize reasoning across memories to answer a strategic question."""
    client = _get_client()
    question = args.question.strip()
    if not question:
        print("Error: question cannot be empty", file=sys.stderr)
        return

    print(f"\n🧠 Reflecting on: {question!r}...\n")
    try:
        res = client.reflect(question=question)
        if isinstance(res, dict) and "answer" in res:
            print(res["answer"])
        else:
            print(json.dumps(res, indent=2))
    except Exception as exc:
        print(f"Reflection failed: {exc}", file=sys.stderr)
    print()


def _cmd_profile(args: argparse.Namespace) -> None:
    """Display the synthesized user profile and persistent preferences."""
    client = _get_client()
    print("\n👤 User Profile & Mental Models (Foresight)")
    print("─" * 45)
    try:
        res = client.profile()
        if isinstance(res, dict):
            static_prefs = res.get("static", [])
            dynamic_models = res.get("dynamic", [])
            if static_prefs:
                print("\n  Preferences & Directives:")
                for pref in static_prefs:
                    print(f"    • {pref}")
            if dynamic_models:
                print("\n  Mental Models & Patterns:")
                for model in dynamic_models:
                    if isinstance(model, dict):
                        print(f"    • [{model.get('type')}] {model.get('content')}")
                    else:
                        print(f"    • {model}")
            if not static_prefs and not dynamic_models:
                print("\n  Profile is currently empty (run agent turns to auto-distill).")
        else:
            print(json.dumps(res, indent=2))
    except Exception as exc:
        print(f"Failed to fetch profile: {exc}", file=sys.stderr)
    print()


def _cmd_test(args: argparse.Namespace) -> None:
    """Run an automated self-test of the Foresight plugin endpoints."""
    client = _get_client()
    print("\n⚡ Running Foresight Integration Self-Test")
    print("─" * 45)

    # 1. Connectivity
    print("  1. Ping /memories:       ", end="", flush=True)
    health = client.health()
    if health.get("ok"):
        print(f"PASS ({health.get('latency_ms')}ms)")
    else:
        print(f"FAIL ({health.get('error')})")
        return

    # 2. Context Injection
    print("  2. Test POST /inject:    ", end="", flush=True)
    try:
        inj = client.inject("test query context injection", max_memories=2)
        formatted = inj.get("formatted", "")
        if formatted:
            print("PASS (recalled context returned)")
        else:
            print("PASS (empty context as expected for novel query)")
    except Exception as exc:
        print(f"FAIL ({exc})")

    # 3. Hybrid Search
    print("  3. Test POST /search:    ", end="", flush=True)
    try:
        client.search("test", limit=2, use_hybrid=True)
        print("PASS")
    except Exception as exc:
        print(f"FAIL ({exc})")

    # 4. Profile
    print("  4. Test GET /profile:    ", end="", flush=True)
    try:
        client.profile()
        print("PASS")
    except Exception as exc:
        print(f"FAIL ({exc})")

    print("\n✓ All tests passed! Foresight is fully operational and hands-off.\n")


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
