# Architecture — proposal, not an implementation claim

```text
User-controlled source exports and connectors
  -> source-specific import + normalization (preserve provenance)
  -> private local data store: raw records, events, notes, source metadata
  -> search / retrieval / optional summaries
  -> bounded, read-only agent interface
  -> separate evaluation and optional forecasting experiments
```

Keep original timestamps, timezone where known, source identity and links to evidence. Track missing data, edits, duplicates and import status; make re-imports idempotent. Derived indexes should be rebuildable. Avoid treating generated summaries or inferred facts as source records. A correction or deletion needs to propagate through derived data, indexes, backups and any approved remote processor; design and test this before real use.

[Tuna's CFV architecture](cfv-architecture.md) covers a local SQLite timeline, source importers, search, and forecasting experiments. [Safa's second-brain architecture](second-brain-architecture.md) describes an existing pipeline for session extraction, curation and retrieval. Each system is a reference for the proposed design. See the [source inventory](source-inventory.md) for the code available in this repo.

## First vertical slice
Use small synthetic examples of two sources, normalize them with citations, retrieve evidence, and answer a question. Then compare on a privately held real dataset once privacy and evaluation procedures are agreed. The initial repo does not implement this slice. WhatsApp imports are a candidate, not a promise of complete or automatic access: assess device, export limits and reliability first.

## Interfaces and decisions
Keep private stores separate for each user. Agent tools should expose only the evidence needed for a request and make read access explicit. Choose storage, model providers, access controls and cloud processing only after documenting the threat and usability tradeoffs. Local storage does not by itself mean local inference: remote model calls can transmit retrieved content.
