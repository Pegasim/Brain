import argparse
from pathlib import Path

from .store import DEFAULT_DB, LookupResult, load_demo, lookup


def main() -> None:
    parser = argparse.ArgumentParser(description="Brain's synthetic calendar and conversation demo")
    parser.add_argument("command", choices=["load", "serve"])
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Path for the synthetic SQLite store")
    args = parser.parse_args()
    if args.command == "load":
        try:
            load_demo(args.db)
        except ValueError as exc:
            parser.error(str(exc))
        print(f"Loaded synthetic fixtures into {args.db}")
        return
    if not args.db.is_file():
        parser.error("Demo database missing. Run the load command first with the same --db path.")

    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    server = FastMCP("brain-demo")

    @server.tool(name="lookup", annotations=ToolAnnotations(readOnlyHint=True))
    def lookup_tool(topic: str, start: str | None = None, end: str | None = None) -> LookupResult:
        """Search Brain's evidence using a short topic like 'demo review'.

        Optional start and end are inclusive local dates (YYYY-MM-DD).
        Cite each record's reference. A calendar plan records what was scheduled.
        A user statement records what was reported. Rescheduling alone leaves
        actual attendance unknown.
        """
        return lookup(args.db, topic, start, end)

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
