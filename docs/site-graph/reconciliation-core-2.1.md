# Graph Evidence Core 2.1 reconciliation

Core 2.1 reconciles source-semantic, artifact, and rendered evidence only after
each lane has produced the frozen evidence contract. Lane failures and revision
mismatches remain explicit; missing evidence is never interpreted as proof that
a route or relationship is absent.

Route identity is canonical and query-free. Same-origin slash variants and
explicitly approved query keys may resolve to one route. Fragments, actions, and
external destinations remain occurrence evidence and never enter page topology.
Conflicting positive/missing evidence and any mismatched-revision evidence use
the `contradicted` state.

Names are display metadata only. Every page retains its exact canonical route,
short and full labels, naming source, confidence, aliases, and deterministic
duplicate-label disambiguation.

Structural analysis distinguishes true orphans from projection-specific
contextual orphans. Menu dependence is calculated by comparing homepage
reachability with and without menu-layer edges. Goal reachability is reported
for both the full and selected projection. Findings reference real occurrence
IDs; trap and bottleneck claims are intentionally absent.

Persistence uses the accepted atomic Core 2.1 batch publication interface.
Interrupted publication rolls back page facts, occurrences, entities, and the
graph snapshot together. This implementation does not run target code, crawl a
live site, contact a provider, change production data, or publish externally.
