# Core 2.1 source-semantic analysis

`extract_source_semantic_evidence` accepts a mapping of repository-relative
paths to source text. Its lane-local `SemanticAdapterResult` is intentionally
shaped for serial reconciliation into the frozen `AdapterResult`,
`LinkOccurrence`, and `CoverageSummary` contracts.

Each occurrence preserves:

- source path, line, column, and statically inferred source route;
- the raw destination expression and any resolved destination;
- resolution state and kind, or an explicit unresolved reason;
- component or router call provenance;
- link-layer classification plus the evidence used for it;
- confidence and a symbol/import provenance chain.

Resolved internal source observations use the `source-only` state. Fragment,
action-scheme, and absolute-network destinations use the explicit `fragment`,
`action`, and `external` non-topology states. Unsupported,
conditional, environment, state, and runtime-only observations use
`unresolved`. The adapter does not assert `confirmed-page`, `redirect`,
`missing`, `artifact-only`, `rendered-only`, `dynamic-unknown`, `contradicted`,
`excluded`, or `unchecked`: those require evidence owned by other Core 2.1
lanes or by serial reconciliation.

Absolute network URLs remain `external` in this source-only lane because it
does not own canonical-host configuration. Serial reconciliation may safely
reclassify a same-site absolute URL when canonical-host evidence agrees.

An optional validated lowercase Git object ID records repository revision
provenance and participates in the batch hash. Output is sorted by source
location and expression. Repeating an analysis with
the same source bytes produces identical evidence order, evidence IDs, and
batch hash. Coverage reports bounded files, bytes, occurrence totals, and
resolved/unresolved totals.

## Current boundary

This lane does not resolve general JavaScript or TypeScript syntax, re-export
graphs, computed property names, nested destructuring, arbitrary callback
bodies, dynamic imports, or framework runtime behavior. Those cases remain
explicitly unresolved when they appear at a destination-bearing call site.
