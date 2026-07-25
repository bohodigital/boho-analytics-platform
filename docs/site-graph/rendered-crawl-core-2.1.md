# Graph Evidence Core 2.1 rendered crawl

`site_graph.adapters.rendered_crawl` supplies a narrow rendered-evidence adapter without making
rendered evidence a global dependency. It is designed for exact owner-authorized targets and
revision-bound snapshots.

## Lane-local contract

The lane exports deterministic forms of `EvidenceBatch`, `PageCandidate`, `PageEntity`,
`LinkOccurrence`, `CoverageSummary`, `AdapterResult`, bounded diagnostics, revision provenance, and
stable SHA-256 hashes. These are intentionally lane-local until serial reconciliation maps them to
the shared schema-4 contracts.

Page and link resolution uses the frozen vocabulary:

`confirmed-page`, `redirect`, `missing`, `source-only`, `artifact-only`, `rendered-only`,
`dynamic-unknown`, `contradicted`, `unresolved`, `excluded`, `unchecked`, `action`, `fragment`, and
`external`.

Capture coverage separately records `complete`, `partial`, `timeout`, `failed`, `blocked`, and
`unchecked`. A failed or unavailable browser leaves rendered evidence unchecked and does not
invalidate accepted source or artifact evidence.

## Captured facts

For desktop and mobile viewports, a successful capture records HTTP status, final URL, canonical,
robots, title, H1 values, schema types, DOM hash, bounded console/network failure classes, and
blocked-resource classes. Anchor and form occurrences preserve text, accessible name, landmark,
visibility, viewport intersection, `rel`, `nofollow`, viewport, resolution, and occurrence
provenance.

The adapter does not merge source, artifact, and rendered facts. Canonical conflicts and revision
mismatches remain explicit for reconciliation.

## Injected browser boundary

Call `crawl_rendered_evidence(authorization, routes, browser_factory)`. The factory receives:

- a fresh temporary profile directory;
- one desktop or mobile viewport;
- the mandatory `RequestPolicy`.

The factory must return a context with `navigate`, `clear_state`, and `close`. It must enforce the
policy on every browser request. Navigation returns a bounded `BrowserCapture`; browser-specific raw
objects never cross the adapter boundary.

The repository includes `scripts/capture_site_graph_evidence.py` for deterministic JSON replay. The
script requires `--owner-authorized`, exact target and revision arguments, a route array, and a
replay document. It intentionally cannot launch a live browser. Run `--help` for the complete
interface.

## Unsupported operations

This lane does not select or bundle a browser, crawl live sites, execute interaction recipes, expand
menus, tabs, accordions, or disclosures, capture screenshots, authenticate, or write to providers.
The shared reconciliation layer handles adapter registration, storage publication, ingest wiring,
and source/artifact/rendered evidence reconciliation.
