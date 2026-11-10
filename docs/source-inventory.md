# Source inventory and provenance

## Safa's second brain
See [second-brain architecture](second-brain-architecture.md) for the existing workflow and the parts that may transfer to Brain. The private system is the source reference. Evaluate and adapt individual components here as needed.

Private source repository: `Safa675/second-brain`. Snapshot commit `7bb41cfc8ce0789ca1a285b10d368e3c08ac244e`. Included: `third_party/second-brain/tools/extract/brain_extract.py`, copied from that committed revision, not the uncommitted working-tree edits. It extracts AI session text and includes redaction and self-tests. It is an unmodified reference snapshot, not yet integrated or generalized. It may contain user-specific assumptions and paths; inspect before use. No personal notes, archive or Git history are included. Further candidates: inbox writer/consolidator and retrieval workflow, after dependency and data review.

## Tuna's CFV
The [CFV architecture review](cfv-architecture.md) describes the upstream implementation and ranks reuse candidates. It is based on [CFV revision `fae1261`](https://github.com/tunapro1234/central-finite-vault/tree/fae1261ec830b4de82dece8304395886668dd8bb). The upstream project has no visible repository-wide license as inspected on 2026-11-10. Before copying code into Brain, agree reuse terms with Tuna, review dependencies and include attribution and the source revision. CFV is currently a documented reference.

## Licensing
The new project's license is undecided. No open-source permission is granted by omission. Set terms with contributors and confirm rights for imported material before public release.
