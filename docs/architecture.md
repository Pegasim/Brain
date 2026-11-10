# Pegasim architecture proposal

Pegasim combines CFV's timeline of what happened with the second brain's curated memory of what matters. The timeline can find a scheduled review and the conversation that followed it. Curated memory can carry forward a decision from that conversation and point back to the evidence. Each person runs their own local store.

## What we take from each system

- **From CFV, take the event model and query patterns.** Normalize records from different apps into a common SQLite timeline, with source IDs, timestamps, local dates and source pointers. Use time windows and text search to find relevant events. Adopt CFV's staged refresh and coverage checks when recurring imports make them useful. [CFV architecture](cfv-architecture.md)
- **From the second brain, take conversation capture and explicit curation.** Use the included extractor as the starting point for synthetic AI sessions. Store durable decisions and preferences as Markdown entries with dated evidence references. Start with hand-written entries. If agents need to propose entries later, adapt the inbox and deterministic filing workflow so proposed and curated memory remain distinct. [Second-brain architecture](second-brain-architecture.md)
- **Build the connection in Pegasim.** Give timeline records stable references and use those references in memory notes. Return both kinds of result through one read interface. Pegasim owns the shared format and can adapt individual components as needed. The existing installations are design references. See the [source inventory](source-inventory.md) for code provenance and upstream reuse terms.

## Data flow

```mermaid
flowchart LR
    Calendar[Calendar export] --> Import[Source adapters]
    Sessions[Extracted AI sessions] --> Import
    Import --> Timeline[SQLite timeline and text index]
    Timeline -. stable evidence references .-> Notes[Curated Markdown notes]
    Timeline --> Retrieve[Read-only lookup]
    Notes --> Retrieve
    Retrieve --> Answer[Answer with source citations]
```

- **Evidence.** Keep source snapshots outside Git. Store normalized events with a type, source-scoped record ID, time and precision, local date, and a pointer to the source. Scope IDs by source instance so identical IDs from different calendars or accounts stay distinct. Keep the normalized timeline and its search index rebuildable. Preserve the distinction between a scheduled event and a record that says it happened.
- **Curated memory.** Each entry states a durable decision or preference, who stated it, when it was recorded, and which event references support it. Markdown is the source of truth for those entries. Retrieval surfaces their evidence alongside the note and identifies a missing source if a reference no longer resolves. A later correction can supersede a note while its earlier evidence remains traceable, subject to the owner's deletion policy.
- **Retrieval.** Start with a local CLI that combines a date range and text query, then follows evidence links from matching notes. Return source type, timestamp, reference and a relevant excerpt. A model can turn that evidence into an answer later. Keep the first lookup deterministic so we can debug retrieval separately from answer generation.
- **Data boundaries.** Each person selects which sources to import. Keep private snapshots, excerpts and stores out of the shared repo. The first build uses synthetic fixtures and runs without a remote model. Before real imports, agree how access and deletion work across the input store, derived data and notes. See [Privacy](privacy.md).

## What to build first

Build one local workflow from **a synthetic calendar export and a synthetic AI session**. This exercises CFV's event model and the second brain's capture pipeline in the same run, using inputs that either collaborator can reproduce.

1. **Import.** Read a small calendar fixture and run the existing extractor on a synthetic agent trace. Normalize both outputs into the same SQLite timeline. Create stable evidence references that survive a reimport or rebuild.
2. **Curate.** Add one hand-written Markdown decision linked to the session evidence. Keep it separate from the timeline records and make it discoverable from the same lookup.
3. **Retrieve and answer.** Query the relevant time range and topic through the CLI. Show the calendar record, the relevant session excerpt, and the linked note. For the first demo, use a fixed expected answer to check the evidence the CLI returns.

Example question. “Was Friday's demo review held as planned, and what did we decide about future reviews?” The calendar shows it scheduled for Friday. The session records the owner saying it was moved to Monday and that future reviews will be on Mondays. The expected answer distinguishes the original plan, the reported change, and the standing decision. It cites the calendar and session, and leaves actual attendance unknown.

The first build passes when both fixtures import twice without duplicate records or broken references, the lookup returns the evidence for all three parts of the answer, and a calendar-only case leaves the outcome unknown. Include an unrelated event in the same time window so the test also checks relevance.

Once this works, try the [memory evaluation](evaluation.md) on private held-out questions and compare with each person's existing workflow. WhatsApp is the next candidate source. Choose between a text export adapter and CFV's compatible iPhone backup importer based on the input both collaborators can use. Add agent access, automated curation, richer identity matching, or forecasting when an experiment needs it.
