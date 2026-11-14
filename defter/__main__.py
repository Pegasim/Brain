import argparse
import os
from pathlib import Path

from .maintenance import run
from .store import DEFAULT_DB, DEFAULT_VAULT, LookupResult, load_demo, lookup, refresh


def main():
    parser = argparse.ArgumentParser(description="Defter: CFV timeline and second-brain memory")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("load", "serve", "refresh"):
        child = commands.add_parser(command)
        child.add_argument("--db", type=Path, default=DEFAULT_DB)
        if command == "refresh":
            child.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
            child.add_argument("--calendar", type=Path, action="append", default=[])
            child.add_argument("--sessions", type=Path, help="Directory of Codex JSONL session traces")
            child.add_argument("--archive", type=Path, help="Redacted conversation Markdown from the extractor")
            child.add_argument("--source-name", default="codex", help="Stable identity for this session source")
    memory = commands.add_parser("memory")
    memory.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
    memory.add_argument("arguments", nargs=argparse.REMAINDER, help="add, inbox, or consolidate and their options")
    args = parser.parse_args()
    try:
        if args.command == "memory":
            raise SystemExit(run(args.vault, args.arguments))
        if args.command == "load":
            load_demo(args.db)
            print(f"Loaded synthetic fixtures through CFV and maintenance tools into {args.db}")
            return
        if args.command == "refresh":
            refresh(args.db, args.vault, args.calendar, args.sessions, args.source_name, args.archive)
            print(f"Refreshed CFV timeline at {args.db}")
            return
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    if not args.db.is_file():
        parser.error("Database missing. Run load for the synthetic demo or refresh to import sources.")

    os.environ["CFV_DB"] = str(args.db.resolve())
    os.environ["CFV_DATA"] = str(args.db.resolve().parent.parent)
    from cfv.mcp_server import mcp
    from mcp.types import ToolAnnotations

    @mcp.tool(name="lookup", annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))
    def lookup_tool(topic: str, start: str | None = None, end: str | None = None) -> LookupResult:
        """Search Defter's CFV timeline and consolidated memory with evidence links.

        Use a short topic and optional inclusive local dates (YYYY-MM-DD).
        Cite returned references. Calendar plans and reported rescheduling leave
        attendance unknown. Missing references mean support could not be retrieved.
        """
        return lookup(args.db, topic, start, end)

    mcp.run()


if __name__ == "__main__":
    main()
