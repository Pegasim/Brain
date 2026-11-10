# Privacy and data boundaries

This project handles the owner's data and other people's messages. Treat the public project repository as code and documentation. Each owner keeps personal data in a separate local store.

- Keep real data, exports, conversation archives, SQLite files, media, credentials, derived indexes, summaries and logs outside the repository. Use synthetic fixtures. Review staged files and commit history before pushing.
- Each person controls a separate local store and decides which sources to import. Do not put another person's private data into a shared test fixture or issue.
- Local storage and local inference are different. No remote model or embedding service should receive personal content by default; explicitly choose and document any external processing, provider, data sent and retention policy first. Minimize the content sent.
- Agent access should be scoped to the task, with source visibility and an understandable way to exclude sources. A read-only interface still exposes information to the agent and potentially its provider.
- Plan deletion across raw copies, derived indexes, summaries, logs, backups and third parties. Do not promise complete deletion until tested.
- Export formats, connectors, retention, encryption, access controls and third-party privacy need design and validation before use on real data. Redaction is helpful but cannot guarantee removal of every secret or identifier.

The included historical extractor is for source review. Its redaction and local path assumptions have not been audited for this project; do not use it as a privacy guarantee.
