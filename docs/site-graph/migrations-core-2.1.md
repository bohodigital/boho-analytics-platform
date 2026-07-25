# Graph Evidence Core 2.1 schema decision

Graph Evidence Core 2.1 remains on the current schema version 4. The historical V2 migration is not
replayed and no new migration is added.

Canonical page facts, link occurrences, page entities, and graph snapshots continue to use the
existing tables. Batch identity, revision relation, unresolved candidates, coverage, and diagnostics
use bounded `site_graph_page_facts.evidence_json` carriers; the graph snapshot content hash is the
batch content hash. Candidate evidence is distributed rather than represented as invented pages.
The old V1 storage methods and readers remain compatible and existing metric and Site Graph data are
unchanged.

## Atomicity and rollback

`SiteGraphStore.publish_evidence_batch` validates references and revision provenance, then writes the
bounded batch and candidate evidence within compatibility page facts, duplicate link occurrences,
page entities, and the graph snapshot in one SQLite transaction. Any exception rolls back every row.
Replaying an identical batch is idempotent.

Because no migration occurs, rollback is the code-level omission of the Core 2.1 commit. Current
backup, restore, schema-3 refusal, integrity checking, WAL, foreign-key, and migration-rollback
behavior is unchanged.

Installing Core 2.1 does not migrate an existing schema-version-4 database.
