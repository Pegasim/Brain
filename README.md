# Defter (working title)

Defter combines CFV's event timeline with the second brain's conversation capture and memory maintenance. An existing agent queries both through a local, read-only MCP server.

The current integration imports calendar exports, Codex traces and redacted conversation archives. It indexes decisions filed through the maintenance inbox and follows their evidence references. Curated notes without log frontmatter, such as indexes, timelines and topic pages, are indexed by their vault path and cited as `note:<path>`; their date comes from a `date` or `updated` field, otherwise from the file's last git commit when the vault is a git checkout, otherwise from its modification time. The bundled demo uses synthetic data.

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

Any stdio MCP host can launch `uv run --locked --offline python -m defter serve` with this repo as its working directory. The available tools are `recall`, `read`, `delta`, `lookup`, `search`, `when`, `timeline`, `digest`, `people`, `photos` and `week_page`. All are read-only. Photo results and weekly pages require the corresponding CFV imports and generated views.

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

To review how agents use the server, set `DEFTER_QUERY_LOG` to a file path. Each `recall`, `read`, `delta` or `lookup` call then appends one JSON line with the time, `DEFTER_CLIENT` (a label you set per agent), the arguments, the number of results, the top references, the latency and any error. The log contains the questions asked, so keep it with the private data.

## Ask in plain language

`recall` answers a question in any wording, English or Turkish. It ranks curated notes first, fusing FTS5 keyword matches with bge-m3 passage similarity by reciprocal rank, then adds conversations that contain every word of four or more letters from the question. Dotted and dotless i and Turkish diacritics are folded, and words longer than five letters match by their first five, so suffixed forms still match.

Passage vectors come from a separate step, so refresh never calls a model:

```sh
uv run python -m defter embed --db data/private/processed/cfv.sqlite
uv run python -m defter recall "When did the review schedule change?" --db data/private/processed/cfv.sqlite
```

`embed` sends note passages of up to 220 words to an OpenAI-compatible `/v1/embeddings` endpoint: by default a local Ollama at `http://localhost:11434/v1/embeddings` with the `bge-m3` model. Set `DEFTER_EMBED_URL` and `DEFTER_EMBED_MODEL` to change them. The endpoint receives note text, so keep it local ([Privacy](docs/privacy.md)). Vectors are cached in the database by passage text and model. A later run embeds only new or changed passages and removes vectors no passage uses. Without vectors, or with the endpoint unreachable, recall returns keyword results with `semantic: false` and a `warning`. Conversations are matched by keyword as whole transcripts and are not embedded.

`read` returns a record's full stored text by a reference from recall, lookup or delta, or a note by its vault path such as `topics/bilun`, in windows of up to 50,000 characters. It works when the agent cannot open the server's files, for example over ssh.

`delta` lists records added, changed or removed since an ISO date or time, oldest first, with a cursor to continue or to ask again later. Each refresh compares every record with the previous one, so change times are refresh times. The first refresh has nothing to compare with and marks every record `initial`, dated by a note's last git commit or otherwise the record's own timestamp.

```sh
uv run python -m defter delta --since 2026-11-13 --db data/private/processed/cfv.sqlite
uv run python -m defter read topics/harbour --db data/private/processed/cfv.sqlite
```

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
