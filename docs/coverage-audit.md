# Pegasim/Brain coverage audit (2026-11-13, read-only)

_Historical record: written when the project was named Brain (repo `Pegasim/Brain`, package `brain_demo`). Renamed to Defter (`Pegasim/defter`, package `defter`) on 2026-11-14; names below are kept as audited._

Repos compared
- Brain: `~/Documents/brain`, origin `Pegasim/Brain`, HEAD 022ed3f = origin/main. GitHub visibility: PUBLIC, no license.
- Tuna: `tunapro1234/central-finite-vault` ("CFV"), PUBLIC, no license. Not on disk; cloned to this session's scratchpad. HEAD c7573e1 (2026-11-12).
- Safa: `~/second-brain` HEAD 5e5dbc9.

Verdict: No. Brain holds a near-complete *copy* of CFV's code and a copy of second-brain's five Python tools, but its own entry point (`brain_demo`, 335 lines) wires only: ICS calendar import, Codex/archive conversation import, add/inbox/consolidate, and 8 read-only MCP tools. Everything that makes second-brain run unattended (timers, sync, hooks, gbrain semantic recall, always-on digest, skills) is absent.

Measured
- `pytest` root + CFV: 148 passed, 2 skipped. `brain.py selftest`: ok (34 checks). `brain_extract.py --selftest`: PASS.
- `brain_demo load` + `lookup` on a scratch DB: returns calendar + note + session with linked references.
- third_party/cfv == upstream fae1261 minus `.mcp.json`, plus 3 local edits (CFV_DB env, server renamed "Brain", read-only annotations). 1 upstream commit behind (c7573e1: akis/tazelik.py photo freshness + tests).
- third_party/second-brain/tools == second-brain HEAD tools, with path/owner generalisation only. second-brain has 1 uncommitted extractor edit (13 lines) not in Brain.
- Not run: Go tests (go not installed), src/predict tests, photos pipeline (cv2, sqlite_vec missing), distiller with a real model.

Second-brain feature -> Brain
| Feature | Source | Brain status |
|---|---|---|
| brain add / inbox / consolidate | tools/brain/brain.py | Working (`brain_demo memory`, brain_demo/maintenance.py) |
| Redaction (keys, TCKN, IBAN, cards) | tools/extract/brain_extract.py | Working; own-identifiers allowlist excluded on purpose |
| Extractor, 7 harness parsers | tools/extract/brain_extract.py | Present, selftest passes; runs standalone only. `refresh --sessions` uses parse_codex only (brain_demo/store.py:36) |
| Archive -> index | ops/gbrain/maint.sh | Working differently: `refresh --archive` into SQLite FTS5 |
| notes/log -> index | gbrain import | Working (ingest_notes) |
| Curated notes (INDEX, timeline, topics, personal) -> index | gbrain import of notes/ | Missing: ingest_notes skips files without `id`+`date` frontmatter (store.py:93) |
| Semantic recall (bge-m3 embeddings), entity, context_pack, delta, synthesize | gbrain MCP + ops/gbrain | Missing. Brain search is FTS5 keyword/prefix only |
| Evidence-linked lookup | none | New in Brain (store.py lookup) |
| Distiller | tools/distill + ops/laptop/brain-distill.* | Code present, dry-run starts; needs `agy`; not scheduled; not tested with a model |
| Digest compiler + harness adapters | tools/compile, core/src, brain adapters | Stub: `compile_core.py --check` -> "no sources in core/src"; adapters -> "source missing" |
| brain doctor / SessionStart hook | brain.py doctor | Partial: runs, but checks assume ~/second-brain layout; no hook config |
| brain skills sync + skills/ | brain.py skills, skills/ | Missing (command exists, no skills dir) |
| brain asset add/get/list/verify/backup | brain_assets.py | Present, selftest passes; not exposed via brain_demo; backup needs a VPS |
| Timers: extract 30m, sync 30m, distill hourly | ops/laptop | Missing |
| VPS nightly consolidate+index 03:30, midday sync 14:00 | ops/gbrain | Missing |
| Hub/GitHub sync, multi-machine | ops/laptop/brain-sync.sh | Missing |
| VPS always-on Claude + capture | ops/vps-agent | Missing |
| Read-only MCP guard (hide remember/forget) | ops/gbrain/serve-ro.py | N/A: Brain MCP has no write tools |

CFV feature -> Brain
| Feature | Source | Brain status |
|---|---|---|
| Timeline schema + FTS5 | src/cfv/schema.sql, db.py | Working, used by brain_demo |
| MCP tools when/search/timeline/digest/week_page/people/photos | src/cfv/mcp_server.py | Working via `brain_demo serve` (+ lookup) |
| ICS calendar import | sources/calendar_ics.py | Working via `brain_demo refresh --calendar` |
| 12 other importers (photos, notes, iOS/Google calendar, reports, contacts, gmail, whatsapp, callhistory, zepp, health, dream) | src/cfv/sources/* | Present, tests pass; reachable only via `cfv ingest`, not via brain_demo |
| cfv CLI (init-db, ingest, tree, search, chart, sync, status, ruya) | src/cfv/cli.py | Present, `cfv --help` works in Brain venv |
| Google Calendar / Gmail sync | gcal_sync.py, gmail_sync.py, src/gsync | Present; needs `gog` (not installed here); untested |
| Dream journal + nightly consolidation | ruya.py, src/akis | Present, tests pass; not scheduled |
| Week tree, chart | tree.py, chart.py | Present; week_page tool returns "no page" until `cfv tree` is run |
| Web site (auth, SMS, matching) | src/site/cfv_site | Present; imports only with PYTHONPATH; tests pass |
| Photos pipeline (Go signals, derive, embed, iCloud download) | src/photos | Present; deps missing here (go, cv2, sqlite_vec); untested |
| Prediction (markov, life2vec, persona, ensemble) | src/predict | Present; own pyproject (torch etc.); untested |
| Go report builder | src/compact | Present; go not installed; untested |
| 13 systemd timers/services | src/systemd, src/site | Present but hard-coded to Tuna's server paths/users |
| .mcp.json | repo root | Excluded on purpose |
| Upstream c7573e1 | akis/tazelik.py | Missing (1 commit behind) |

Gaps ranked
1. No semantic recall / entity / context_pack; curated notes not indexed.
2. No automation at all (timers, hooks, nightly job, sync).
3. Always-on digest, adapters, skills not functional.
4. Only Codex raw traces wired; other harnesses need a manual extractor run.
5. CFV importers beyond ICS not wired into Brain's refresh; CFV ops units unusable outside Tuna's server.
6. Distiller unverified end to end.
7. 1 CFV commit behind; 1 uncommitted second-brain extractor edit not carried.
Side finding: Pegasim/Brain is PUBLIC and carries CFV code with no license; notes of 2026-11-10/11 say "private repo" and "get Tuna's reuse terms before importing". Safa's VPS IP is not in the tree or history.
