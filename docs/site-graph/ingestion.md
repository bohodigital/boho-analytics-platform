# Repository ingestion

Site-graph ingestion is a local, operator-triggered CLI workflow. The dashboard never opens a
repository or starts an ingest.

## Supported source adapters

- `static-html` maps tracked HTML files to routes and parses every anchor occurrence. Explicit
  `data-link-layer` values take precedence, followed by semantic breadcrumbs, related-content and
  CTA markers, footer/header/navigation landmarks, and a contextual fallback.
- `vinext` discovers App Router pages plus route and link declarations in tracked `app/` and `src/`
  TypeScript, JavaScript, JSX, TSX, and MDX files. Source-only classifications are labeled as bounded
  heuristics with confidence values. Navigation declarations are preserved as repeated menu
  occurrences across discovered pages. A project-level `app/content/publicPages.ts` retired-route
  set is honored when present.
- `auto` selects vinext or Astro from `package.json`, otherwise static HTML. Astro detection is
  reported, but extraction currently fails closed until its adapter is implemented.

Source-only evidence describes repository structure. It does not claim that a route was deployed,
crawled, visited, or converted.

Graph Evidence Core 2.1 can reconcile three bounded evidence lanes at one exact repository revision:

- source-semantic extraction from tracked source;
- artifact evidence from authorized, locally supplied build/deployment artifacts;
- rendered-crawl evidence from an explicitly authorized origin or deterministic replay.

Unavailable lanes remain failed or unchecked; revision mismatches and contradictions remain visible.
Reconciliation never promotes action, fragment, external, unresolved, or contradicted destinations
into topology. Publication is atomic and carries complete route-resolution coverage into the
existing immutable fact tables.

## Safe workflow

1. Copy `examples/site-graph/vinext-site.yaml` or `static-site.yaml` to a private path.
2. Replace placeholders with an owner-authorized absolute repository path, the exact origin URL,
   ref, and full commit. Keep credentials and provider tokens out of the manifest.
3. Run `manifest validate` and `inspect-repo`.
4. Back up the analytics database, then run `ingest` with its explicit database path.
5. Run `compile` for the required projection and verify `report` before opening `/site-graph`.

```console
boho-analytics site-graph manifest validate --manifest site-graph.yaml
boho-analytics site-graph inspect-repo --manifest site-graph.yaml
boho-analytics site-graph ingest --manifest site-graph.yaml --database var/analytics.sqlite3
boho-analytics site-graph compile --database var/analytics.sqlite3 --site example-site --projection contextual
boho-analytics site-graph report --database var/analytics.sqlite3 --site example-site --latest --format json
```

The CLI output contains the sanitized repository identity, exact revision, adapter version, manifest
hash, content hash, counts, layer coverage, and whether an existing immutable snapshot was reused.
For reconciled snapshots, `site-graph report` also exposes complete Core 2.1 candidate, entity,
relationship, resolution-state, contradiction, exclusion, and corrected structural summaries. It
does not emit the local repository path, source excerpts, provider references, or credential values.

## Limits and failure behavior

Inspection rejects mismatched origins or revisions, dirty worktrees by default, symlink entries,
unsafe paths, oversized trees, oversized source files, excessive total source bytes, unsupported
adapters, and build mode without a network-isolated runner. Ingest failure marks the run failed and
does not convert partial facts into a successful repository snapshot.
