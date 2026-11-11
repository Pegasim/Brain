# tools/extract

`brain_extract.py` turns agent session traces into conversation-only, redacted markdown (one file per session).

- Adapters: Claude Code (`~/.claude/projects/*/*.jsonl`), Codex (`~/.codex/sessions/**/rollout-*.jsonl`),
  OpenCode (`opencode.db`). Sub-agent / forked threads and trivial sessions are dropped by design.
- Keeps user and assistant text plus one line per tool call. Drops tool outputs, images, reasoning, injected context.
- Deterministic: no extraction timestamps, so an unchanged session produces no git diff.
- Redaction runs before write (API keys, tokens, URL passwords, `*_KEY=`/`password=` assignments, TC kimlik with
  checksum, TR IBAN, Luhn card numbers). `python3 brain_extract.py --selftest` must pass before a change ships.
- Standard library only, Python 3.8+.

```
python3 -I brain_extract.py --out ~/second-brain-archive --since-days 14
```

Manual one-off imports: `--claude-web-export conversations.json` and
`--harness gemini --gemini-takeout "Takeout/My Activity/Gemini Apps/MyActivity.json"`. Takeout media stays out of the
archive (attachment names only). After importing, add the new files to `done` in the live distiller's `state.json`,
or its hourly run dates them today; distill them with `brain_distill.py --history-since ... --history-harness gemini`.

Scheduling lives in `ops/laptop/` (laptop) and `ops/gbrain/` (VPS index). Output goes to the separate
`second-brain-archive` repo, which is never pushed to GitHub.
