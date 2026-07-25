# Graph Evidence Core 2.1 contracts

Graph Evidence Core 2.1 freezes a small adapter boundary without coupling evidence acquisition to
SQLite. Adapters return `AdapterResult`; successful and partial results contain one deterministic
`EvidenceBatch`. A batch contains `PageCandidate`, `PageEntity`, `LinkOccurrence`, and
`CoverageSummary` records plus bounded diagnostics and exact repository/deployment revision
provenance.

## Resolution states

The contract accepts exactly:

```text
confirmed-page  redirect       missing       source-only
artifact-only   rendered-only  dynamic-unknown
contradicted    unresolved     excluded      unchecked
action          fragment       external
```

Unknown evidence remains unknown. `action`, `fragment`, and `external` occurrences cannot carry a
canonical page destination and are never topology eligible. Revision evidence is separately and
explicitly `exact`, `mismatch`, or `unchecked`; mismatched revisions are never silently combined.

## Identity and ordering

Candidate, page, occurrence, coverage, batch, adapter-result, and Core 2.1 graph snapshot identities
are SHA-256-derived from normalized public inputs. Batch normalization sorts records by stable ID, so
input traversal order does not affect hashes or IDs. Duplicate identities and dangling references
fail before persistence.

Analytical coverage totals are independent of emitted or displayed row counts. A result may report
more total routes, pages, or relationships than it emits in a bounded view, but it cannot claim a
total smaller than the evidence it actually emitted.

## Schema-4 compatibility matrix

| Core 2.1 concept | Current structure | Decision | Immediate consumer |
| --- | --- | --- | --- |
| repository/deployment provenance | repository snapshots and ingest runs | reuse | every adapter batch |
| evidence batch identity, coverage, revision relation | page-fact evidence JSON plus snapshot hash | reuse bounded carrier | reconciliation and coverage reporting |
| page candidate, including unresolved candidates | page-fact evidence JSON | reuse bounded distributed carrier | reconciliation |
| canonical page fact | `site_graph_page_facts` | reuse | analysis and V1 readers |
| duplicate link occurrence | `site_graph_link_occurrences` | reuse | analysis and inspectors |
| page display entity | `site_graph_page_entities` | reuse with `core21-page` type | naming and inspectors |
| graph snapshot | `site_graph_snapshots` | reuse with batch content hash | atomic publication |
| page roles | `site_graph_page_roles` | handled by reconciliation | reconciliation readers |
| component/interaction records | none | unsupported | no current consumer |
| analytics overlays | metric facts | kept separate | aggregate reporting only |

Candidate evidence is distributed deterministically across the batch's canonical page facts. The
first carrier also stores coverage and bounded diagnostics. A batch with evidence but no canonical
page carrier fails closed instead of inventing a page. No decorative component, recipe,
rendered-state, entry-observation, parallel table set, or parallel graph database is added.

## Privacy and bounds

All text and nested JSON are bounded. Controls, user-home paths, email-shaped values, common secret
formats, credential keys, raw-query keys, session/visitor/distinct-ID fields, IP-address fields, and
user-agent fields fail closed. Diagnostics allow only severity, code, and message and are capped at
100 entries of 500 message characters each.

The contract never implies that missing evidence proves absence. It stores aggregate, public-safe
structural evidence only.
