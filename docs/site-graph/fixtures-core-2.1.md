# Graph Evidence Core 2.1 fixture contract

This public-safe synthetic fixture is the analyzer-independent ground truth for Graph Evidence Core 2.1. It recovers the approved contract-fixture behavior from the Site Graph V2 salvage without importing its analyzer, migration, storage, reporting, or runtime implementation.

## Files

- `tests/site_graph/fixtures/core21/public_core21_contract.yaml`
- `tests/site_graph/ground_truth/core21/public_core21_ground_truth.yaml`
- `tests/site_graph/test_core21_fixture_contracts.py`
- matching public examples below `examples/site-graph/`

The test and example YAML files must remain exactly equivalent. The validator imports no production
Site Graph module, keeping the public contract independently reviewable.

## Contract boundary

The fixture uses only reserved `.example` origins, synthetic revisions, invented routes, and synthetic source locations. It contains no customer content, provider identifiers, credentials, visitor or session identifiers, raw analytics queries, private hosts, local paths, runtime state, or production configuration.

Every link-like fact has a unique occurrence key and exact evidence provenance. Duplicate source/destination pairs remain separate occurrences. Missing, unknown, unchecked, excluded, action, fragment, and external evidence has no page-to-page target. A validator regression test prevents an undeclared candidate from becoming a page.

The frozen resolution vocabulary is:

```text
confirmed-page
redirect
missing
source-only
artifact-only
rendered-only
dynamic-unknown
contradicted
unresolved
excluded
unchecked
action
fragment
external
```

Evidence from source, artifact, and rendered capture carries its own revision. Disagreement is explicit; revision-associated facts are never silently merged.

## Scenario coverage

The catalog covers direct and wrapped links; route registries; nested props; bounded arrays, maps, and spreads; static conditions; dynamic routes; duplicate occurrences; fragments; actions; redirects; canonical conflicts; source/artifact/rendered disagreement; unresolved expressions; menu/home dependence; healthy and trapped strongly connected components; dominators and gateways; alternate goal paths; snapshot changes; invented-route regressions; interrupted publication; projection consistency; and visual limits versus analytical totals.

The ground truth also requires:

- added, changed, and removed snapshot differences;
- atomic visibility of the previous complete snapshot after an interrupted publication;
- consistent totals for all-internal, contextual, and navigation projections;
- both a goal-reaching SCC and a trapped SCC;
- two edge-disjoint paths to the synthetic goal;
- display totals larger than the bounded visual limits, with truncation disclosed.

## Intended consumers

Adapters and the reconciliation core may consume these files as executable contracts, but must not tailor the fixture to one parser’s incidental output. Unsupported runtime, state, or environment expressions stay unresolved. Visual limits govern display only; storage, analysis, tables, and exports retain the analytical total.
