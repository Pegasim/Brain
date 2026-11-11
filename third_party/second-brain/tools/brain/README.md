# tools/brain

`brain.py` is the one CLI behind the write path, the consolidator, the doctor and the harness adapters. Standard
library only (Python 3.8+). On the laptop it is on PATH as `brain` (symlink in `~/.local/bin`).

| Command | What it does |
|---|---|
| `brain add --kind K --topic T --title … --context … --result … [--evidence …] [--next …]` | Writes one validated, redacted entry to `inbox/`. Identical entries are no-ops. Format: `inbox/README.md`. |
| `brain inbox` | Lists pending, invalid and consolidated entries. |
| `brain consolidate [--commit] [--push]` | `inbox/` -> `notes/log/<yyyy>/<date>-<topic>-<slug>-<id>.md`, inbox file moved to `inbox/_done/<yyyy-mm>/`. Deterministic: no LLM, no API key. Runs in the VPS nightly job; the only writer of `notes/log/`. |
| `brain doctor [--hook] [--json]` | Digest freshness, repo sync with hub/GitHub, inbox backlog and invalid entries, archive sync, systemd timers, cached VPS nightly status, adapters, skills. `--hook` prints a short context block for a SessionStart hook and always exits 0. |
| `brain skills [list\|sync]` | Links the skills in `skills/ACTIVE` into `~/.claude/skills` (symlinks; removes links it owns that left the list). |
| `brain adapters [--install] [--force]` | Checks or installs the harness wiring. Instructions: `core/adapters/AGENTS.md` / `GEMINI.md` as symlinks for Claude Code (`~/.claude/CLAUDE.md` -> `AGENTS.md`), Codex, OpenCode, Gemini CLI and Antigravity (`~/.gemini/config/GEMINI.md`, its global customization root). `--force` replaces a regular file at a target with the link and keeps it as `<name>.bak-<date>` beside it; `brain doctor` reports any target that is not the link. Recall: adds the `gbrain` MCP server (read-only entry point `serve-ro.sh`) to Codex (`codex mcp add`), OpenCode (`opencode.jsonc`), Antigravity (`~/.gemini/config/mcp_config.json`) and Muse Code (`~/.config/muse/settings.json`, `mcpServers`, stdio, `mode: optional`). Muse Code needs no instruction link: it imports `~/.claude/CLAUDE.md` itself (verified 2026-10-28). Idempotent; every edited config is backed up to `~/.cache/brain/backups/` (0600) first, and an edit that would not parse is not written. |
| `brain vps-cache` | Refreshes the cached VPS status that `doctor` reads (called by `ops/laptop/brain-sync.sh`). |
| `brain selftest` | Built-in tests in a temporary repo. Must pass before a change ships. |
| `brain asset add FILE [--title …] [--note …] [--describe …] [--topic …] [--captured …] [--sensitive]` | Stores a binary file in `~/brain-assets/<topic>/<captured date>-<title>.<ext>` (outside git; no date prefix when `--captured` is not given; no topic means `unsorted/`) and writes a text card to `notes/files/`, which the normal index searches. The card keeps the file's hash and its `path:` inside the store. `--describe` text is marked model-made. `--sensitive` keeps extracted text out of the card. |
| `brain asset get ID` / `list` / `verify` | Prints a file's local path so a model can look at it / lists assets / checks every file against the hash in its card. |
| `brain asset backup [--if-changed]` / `restore [--target DIR]` | Plain rsync copy on the VPS (`/root/brain-assets`, root-only and unencrypted, by Safa's choice); `brain-sync` runs it. The copy only adds files; it never deletes on the VPS. |

## Design

- **Agents propose, one process disposes.** Writers never touch `notes/`; they add inbox files with unique names, so
  concurrent harnesses cannot conflict. The consolidator is the single writer of `notes/log/`.
- **Mechanical on purpose.** Consolidation validates, redacts, dedupes and files; it does not paraphrase. Promoting
  log entries into the curated notes (`INDEX.md`, `timeline.md`, `topics/`) is an editorial act done as a normal edit
  and commit, by Safa or by an agent with Safa watching.
- **Doctor never touches the network in the hook.** The laptop sync timer caches the VPS status; the hook only reads
  local files and git refs, so a session never waits on the VPS.
