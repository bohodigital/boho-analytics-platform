# Site graph architecture

The site graph is a package submodule of Boho Analytics Platform. It reuses the existing Python distribution, command-line entry point, and SQLite database. It is not a Git submodule, second repository, service, or database.

## v1 boundary

The package establishes three durable layers:

1. A strict YAML manifest describes the expected repository, bounded analysis mode, route policy, page-role rules, link-layer selectors, and goals.
2. Schema version 2 stores immutable source facts and derived graph artifacts with foreign-key links to the exact manifest and repository revision that produced them.
3. A dependency-free compiler and read-only dashboard turn completed facts into honestly bounded structural views without exposing repository paths, remote URLs, credentials, or source excerpts to the browser.

Source facts are deliberately more detailed than graph edges. Every matching link occurrence is retained with its source location, landmark, classification layer, confidence, flags, and structured evidence. Later graph compilation may aggregate those occurrences, but it cannot replace them.

The main provenance chain is:

```text
manifest version -> ingest run -> repository snapshot -> page/link facts
        |                                      |
        +---------------- graph snapshot ------+
                              |
                 metrics, components, findings
```

IDs are deterministic where records are immutable. Replaying the same fact batch is idempotent; reusing an immutable key for different content fails. A batch containing a link with an unknown source page rolls back as one transaction.

## CLI

Validate a manifest without loading platform credentials or the normal analytics configuration:

```console
boho-analytics site-graph manifest validate --manifest examples/site-graph/static-site.yaml
```

The command returns only a sanitized JSON summary. Repository paths, remote URLs, and external account references are not emitted.

Inspect the authorized Git worktree, persist the exact source facts, and compile them:

```console
boho-analytics site-graph inspect-repo --manifest site-graph.yaml
boho-analytics site-graph ingest --manifest site-graph.yaml --database var/analytics.sqlite3
boho-analytics site-graph compile --database var/analytics.sqlite3 --site example-site --projection contextual
```

Inspection requires the worktree root, exact origin URL, ref, full revision, and clean status declared
by the manifest. It reads Git objects and never checks out, resets, builds, or writes to the source
repository. A repeated ingest for the same manifest and revision reuses the immutable snapshot.

Compile the latest successful ingest for a site and read the same normalized summary used by the
dashboard:

```console
boho-analytics site-graph report --database var/analytics.sqlite3 --site example-site
boho-analytics site-graph report --database var/analytics.sqlite3 --site example-site --page /services/
```

Compilation is deterministic for an exact repository snapshot, manifest, projection, goal
definition, and fact content. The contextual projection includes `contextual`, `related`, and
`action` link occurrences while excluding `menu`, `breadcrumb`, and `utility` occurrences from
contextual reachability metrics. Distinct occurrences remain linked to their immutable evidence even
when a structural edge aggregate is emitted.

The compiler currently records in-degree, out-degree, goal distance, internal authority, menu
dependence, strongly connected components, orphans, contextual dead ends, and menu-dependence
findings. Component analysis is iterative so large acyclic sites do not hit Python's recursion limit.

See [site graph engine](engine.md) for the full manifest, ingestion, compilation, display, and
public/private-boundary documentation.

## Dashboard

`/site-graph` is part of the existing loopback server. It uses the same Host allowlist, Basic-auth
option, restrictive CSP, no-CORS policy, `no-store` response policy, and credential isolation as the
analytics reports. `/api/v1/site-graph` returns the same normalized read model as JSON.

The dashboard uses two explicit rendering modes. A selected graph containing no more than 36 nodes
and 60 resolved unique edges automatically uses full-graph mode. Larger graphs use bounded mode and
render at most 36 nodes and 60 unique edges. A unique edge is the aggregate of matching occurrences
with the same source canonical route, destination canonical route, and layer. The safe thresholds are
browser-rendering limits, not storage, compiler, table, or export limits.

Every graph response discloses displayed and total node counts, displayed and total resolved unique
edge counts, represented and total selected link-occurrence counts, unresolved relationship count,
active projection, active layers and filters, aggregation mode, whether truncation occurred, and each
reason it occurred. A page selection requests a bounded two-hop neighborhood. Its legacy candidate
walk inspects at most 5,000 selected occurrences and the response explicitly discloses when that cap
could affect the neighborhood.

The server-rendered complete edge table is independent of the SVG limit. It reads 1,000 aggregated
rows per page and supports parameterized route/layer filtering plus allowlisted source, destination,
layer, or occurrence-count sorting. Pretty names are derived from canonical routes because the
current page fact contract does not store titles. `/api/v1/site-graph.csv` streams all matching
resolved unique edges in 500-row database pages. Neither path exposes raw source locations or
repository identity. The default selected layers remain `contextual`, `related`, and `action`;
navigation layers can be enabled explicitly.

The SVG layout is an organic, deterministic topic-cluster map. It uses section anchors, stable
hash-based jitter, collision relaxation, curved directed edges, quiet navigation-layer styling, key
page labels, and a plain-language map hint so non-graph users can read it as pages and pathways
rather than a mathematical graph.

The graph viewport supports bounded wheel and toolbar zoom plus pointer panning. Client coordinates
are transformed through the SVG screen matrix so interaction remains aligned when responsive layout
introduces letterboxing. Pointer capture is released on every drag exit path, and Escape clears a
pinned selection whether it was established by keyboard or pointer.

Every SVG has a title and description. A server-rendered node/edge fallback and the complete edge
table expose equivalent values without JavaScript for keyboard and screen-reader use. Graph mode,
table filtering, sorting, pagination, and CSV export are ordinary same-origin links and forms.

The page labels all outputs as structural evidence. Link topology does not prove visits, attention,
intent, conversion, or revenue impact.

## Database operations

The normal database commands apply the site-graph migration and retain the existing WAL, integrity-check, backup, and confirmed-restore behavior:

```console
boho-analytics --config platform.toml db init
boho-analytics --config platform.toml db check
```

Schema version 2 upgrades a version-1 analytics database in place. Back up a production database before upgrading, and use the existing confirmed restore flow to roll back.
