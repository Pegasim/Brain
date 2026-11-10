# Brain (working title)

An exploration of a personal system built from a person's digital history. It could give agents useful context, help people explore their own past, and eventually test whether a system can represent or forecast aspects of its owner. The product identity is deliberately undecided.

**Status:** synthetic calendar and conversation demo over MCP.

## Run the demo

From the repository root, with Python 3.11+ and `uv`, load the synthetic fixtures:

```sh
uv run --locked python -m brain_demo load
```

Start Codex with the local MCP server for this session:

```sh
codex -c 'mcp_servers.brain_demo.command="uv"' \
  -c 'mcp_servers.brain_demo.args=["run","--locked","--offline","python","-m","brain_demo","serve"]'
```

Ask: “Was the October 30 demo review held as planned, and what did we decide about future demo reviews? Use Brain's lookup tool and cite the evidence.” The calendar shows the Friday plan, the session records the owner's report that it moved to Monday, and the curated note records the decision to hold future reviews on Mondays. Attendance is unknown.

For another MCP host, launch `uv run --locked --offline python -m brain_demo serve` from the repository root as a local stdio server. The server exposes one read-only `lookup` tool. The loader writes to `data/brain-demo.sqlite`, which Git ignores. It accepts only the bundled synthetic sources.

Run the protocol checks with `uv run --locked python -m unittest discover -s tests -v`.

## Project docs

- [Project thesis](docs/thesis.md) — ambition, candidate positions and decisions
- [Architecture](docs/architecture.md) — proposed data flow and source projects
- [Second-brain architecture](docs/second-brain-architecture.md) — how Safa's existing system captures, curates and serves memory
- [CFV architecture](docs/cfv-architecture.md) — how Tuna's timeline works, what it adds, and what Brain could reuse
- [Evaluation](docs/evaluation.md) — experiments and baselines
- [Privacy](docs/privacy.md) — practical data boundaries
- [Landscape](docs/landscape.md) — comparison questions and research plan
- [Source inventory](docs/source-inventory.md) — code provenance and reuse status

The code in `third_party/` is a source snapshot for review, not an integrated or supported application. Use only synthetic data while developing. Never commit personal exports, archives, databases, credentials, generated summaries, or logs.
