# Defter (working title)

Defter combines CFV's event timeline with the second brain's conversation capture and memory maintenance. An existing agent queries both through a local, read-only MCP server.

The current integration imports calendar exports, Codex traces and redacted conversation archives. It indexes decisions filed through the maintenance inbox and follows their evidence references. Curated notes without log frontmatter, such as indexes, timelines and topic pages, are indexed by their vault path and cited as `note:<path>`; their date comes from a `date` or `updated` field, otherwise from the file's modification time. The bundled demo uses synthetic data.

## Run the demo

Use Python 3.12+, SQLite 3.43+ with FTS5, and uv. From the repository root:

```sh
uv sync --locked
uv run python -m defter load
```

The demo runs CFV's ICS importer, parses the conversation with the second-brain extractor, files a decision through its inbox and consolidator, and builds a CFV database at `data/processed/cfv.sqlite`.

Start Codex with Defter for this session:

```sh
codex -c 'mcp_servers.defter.command="uv"' \
  -c 'mcp_servers.defter.args=["run","--locked","--offline","python","-m","defter","serve"]'
```

Ask “Was the October 30 demo review held as planned, and what did we decide about future demo reviews? Use lookup and cite its references.”

The returned records describe the original plan, the owner's reported rescheduling and the standing Monday decision. Actual attendance is unknown. Ask about the November 6 release review to exercise the calendar-only case.

Any stdio MCP host can launch `uv run --locked --offline python -m defter serve` with this repo as its working directory. The available tools are `lookup`, `search`, `when`, `timeline`, `digest`, `people`, `photos` and `week_page`. Photo results and weekly pages require the corresponding CFV imports and generated views.

## Use a private vault

Choose separate paths for personal data. These commands assume an explicitly selected local calendar file and directory of redacted conversation Markdown produced by the imported extractor:

```sh
uv run python -m defter refresh \
  --db data/private/processed/cfv.sqlite \
  --vault data/private/vault \
  --calendar /absolute/path/calendar.ics \
  --archive /absolute/path/redacted-conversations \
  --source-name archive
```

Use `--sessions /absolute/path/codex-traces --source-name laptop` for raw Codex traces in the extractor's supported format. Each source name identifies one capture directory. Reimport the same directory under the same name. Calendar ingestion uses CFV's existing ICS recurrence and timezone handling.

Write a durable entry with the imported maintenance CLI. Replace the example evidence reference with one returned by lookup:

```sh
uv run python -m defter memory --vault data/private/vault add \
  --kind decision --topic reviews --title "Monday review schedule" \
  --context "The owner changed the review schedule." \
  --result "Future reviews are scheduled on Mondays." \
  --evidence session:archive:codex:SESSION_ID
uv run python -m defter memory --vault data/private/vault consolidate
uv run python -m defter refresh \
  --db data/private/processed/cfv.sqlite --vault data/private/vault
```

Pending inbox entries become searchable after consolidation and refresh. Rebuilding selected sources retains the other sources in the database. Failed staged imports leave the published snapshot intact. A deleted supporting record appears in lookup's `missing_references`. A private refreshed database is protected from replacement by the synthetic loader.

Serve that store with `uv run python -m defter serve --db data/private/processed/cfv.sqlite`. The server reads the selected database. The host agent and its model receive the retrieved excerpts.

The distillation and context compiler tools are imported under `third_party/second-brain/tools/`. The distiller writes through the bundled maintenance CLI into the vault selected by `BRAIN_REPO`. Its archive and state paths use `BRAIN_ARCHIVE` and `BRAIN_DISTILL_STATE`, with defaults under this repo's ignored `data/` directory. Running it requires a configured model backend. Scheduled jobs and model calls are operator actions.

## Verify

```sh
uv run python -m unittest discover -s tests -v
uv run pytest -q third_party/cfv/tests
uv run python third_party/second-brain/tools/brain/brain.py selftest
uv run python third_party/second-brain/tools/extract/brain_extract.py --selftest
```

## Project docs

- [Architecture](docs/architecture.md)
- [Project thesis](docs/thesis.md)
- [Second-brain architecture](docs/second-brain-architecture.md)
- [CFV architecture](docs/cfv-architecture.md)
- [Evaluation](docs/evaluation.md)
- [Privacy](docs/privacy.md)
- [Landscape](docs/landscape.md)
- [Source inventory and local adaptations](docs/source-inventory.md)

Keep personal exports, archives, vaults, media, databases, credentials and generated logs outside Git. All committed fixtures are synthetic.
