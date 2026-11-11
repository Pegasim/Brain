# Source inventory and provenance

## Tuna's CFV

Imported under `third_party/cfv/` from `tunapro1234/central-finite-vault` at commit `fae1261ec830b4de82dece8304395886668dd8bb`.

The snapshot includes application source, tests, upstream documentation, dependency metadata and Go modules. It retains CFV's timeline schema, source importers, query and MCP layers, staged maintenance pipeline, web interface, report builder, media tools and forecasting experiments. Brain installs this local CFV package as a dependency. The upstream deployment configuration in `.mcp.json` was excluded.

Local adaptations apply to the imported package. `cfv.db.default_db_path` accepts `CFV_DB` so the shared MCP server reads the explicitly selected store. The MCP server identifies itself as Brain and uses a generic owner description. Its read-only tools declare read-only MCP annotations for host approval policies.

The upstream project has no repository-wide license in the inspected revision. Preserve its authorship and bundled third-party notices. Importing the snapshot does not establish an open-source license for it. Distribution terms remain to be agreed with Tuna.

## Safa's second brain

Imported under `third_party/second-brain/tools/`. The extractor's original snapshot is from `Safa675/second-brain` commit `7bb41cfc8ce0789ca1a285b10d368e3c08ac244e`. The maintenance CLI, asset tool, context compiler, distiller and tool READMEs are from commit `0e7aba424499aa141c1d7a302f01126ccd7b0b8f`. This import uses committed code. Safa's existing working-tree edits remain in the private source checkout.

Local adaptations resolve the redactor relative to the bundled tools, replace a personal VPS address with a configurable placeholder, generalize the distillation prompt, place its defaults under Brain's private data directory, and direct distillation writes through the bundled CLI. The extractor's Codex parser now closes its input file explicitly.

The active integration uses conversation parsing/redaction and the add/inbox/consolidate path. Asset tooling, the compiler and the configurable distiller are available for explicit use. The private notes, conversation archive, identity-number allowlist, Git history and personal deployment jobs are outside this import.

## Brain integration

`brain_demo/` contains the new adapters and runtime entry point. It uses CFV's ICS importer, `Batch`, schema and search, and the second-brain extractor and maintenance CLI. Source-scoped stable references connect consolidated notes to conversations. A small `brain_reference` table adds those references to the CFV database.

The previous miniature `record` database is replaced by CFV's schema. The MCP entry point exposes CFV's query tools plus the cross-source `lookup` tool. The frontend, media processing and forecasting source are included in the snapshot and have their own upstream entry points. Brain's current entry point integrates capture, maintenance and retrieval.

## Verification and scope

Root integration tests cover the MCP tools, repeated import, retained calendar sources, failed-refresh isolation, private-store overwrite protection, unresolved citations after deletion and extracted-archive ingestion. CFV's tests use synthetic fixtures. Optional external tools and model-backed workflows require their own setup before use.
