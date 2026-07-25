# Source-semantic security boundary

The Core 2.1 source-semantic adapter is a static evidence collector. It reads
already supplied text and does not import target modules, invoke package
scripts, evaluate expressions, start builds, access the network, or inspect
runtime state.

The supported subset is intentionally small: quoted literals, statically
resolved constants and aliases, identifier-only template substitutions,
allowlisted route-helper calls, bounded object/array properties, JSX route
attributes, router calls, and bounded `map`/`flatMap` projections. Conditional,
environment, state, unknown-helper, and other runtime expressions produce
ordered `unresolved` evidence with a reason. They never produce a guessed
destination.

Input paths, individual files, aggregate bytes, resolution depth, and evidence
counts are bounded. Unsafe paths and limit violations fail closed. Evidence IDs
and the batch hash are derived from canonical JSON; input mapping order cannot
change the result.

The adapter emits no secrets beyond source expressions that the caller already
authorized for analysis. Downstream displays should treat raw expressions as
source material and apply their normal repository-access controls.
