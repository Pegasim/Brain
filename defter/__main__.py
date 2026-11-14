import argparse
import json
import os
import sys
from pathlib import Path

from .maintenance import run
from .recall import RecallResult, embed, recall
from .store import DEFAULT_DB, DEFAULT_VAULT, LookupResult, load_demo, lookup, refresh


def main():
    parser = argparse.ArgumentParser(description="Defter: CFV timeline and second-brain memory")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("load", "serve", "refresh", "embed", "recall"):
        child = commands.add_parser(command)
        child.add_argument("--db", type=Path, default=DEFAULT_DB)
        if command == "refresh":
            child.add_argument("--vault", type=Path, default=DEFAULT_VAULT)
            child.add_argument("--calendar", type=Path, action="append", default=[])
            child.add_argument("--sessions", type=Path, help="Directory of Codex JSONL session traces")
            child.add_argument("--archive", type=Path, help="Redacted conversation Markdown from the extractor")
            child.add_argument("--source-name", default="codex", help="Stable identity for this session source")
        if command == "recall":
            child.add_argument("query")
            child.add_argument("--start")
            child.add_argument("--end")
            child.add_argument("--limit", type=int, default=8)
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
        if not args.db.is_file():
            parser.error("Database missing. Run load for the synthetic demo or refresh to import sources.")
        if args.command == "embed":
            report = embed(args.db, progress=lambda done, total: print(f"\r{done}/{total}", end="", file=sys.stderr))
            print(f"\nEmbedded {report['embedded']} new passages with {report['model']}; "
                  f"{report['passages']} note passages, {report['pruned']} unused vectors removed")
            return
        if args.command == "recall":
            print(json.dumps(recall(args.db, args.query, args.start, args.end, args.limit), ensure_ascii=False, indent=1))
            return
    except (ValueError, OSError) as exc:
        parser.error(str(exc))

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

    @mcp.tool(name="recall", annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False))
    def recall_tool(query: str, start: str | None = None, end: str | None = None, limit: int = 8) -> RecallResult:
        """Answer a natural-language question from memory: curated notes first, then conversations.

        Ask in any wording, English or Turkish. Notes are ranked by keywords and meaning together;
        `matched` says which. Optional inclusive local dates (YYYY-MM-DD) filter by the note's date.
        Cite returned references and open `source` for the full text. If `semantic` is false,
        results are keyword matches only and `warning` says why.
        """
        return recall(args.db, query, start, end, limit)

    mcp.run()


if __name__ == "__main__":
    main()
