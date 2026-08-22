# Site graph engine

The site graph engine turns a checked Git repository into structural evidence about a website. It
does not crawl the public internet, run analytics queries, infer user behavior, or execute arbitrary
project commands. Its job is narrower and more auditable: inspect an authorized source tree, retain
immutable page and link facts, compile deterministic graph snapshots, and expose bounded read-only
views that are safe for a loopback dashboard.

This document describes the engine as a public, reusable component. Examples use placeholder paths,
repository names, and site identifiers. Keep account-specific manifests, database files, credentials,
resource IDs, and non-public deployment records outside the public repository.

## Design goals

- Preserve evidence before interpretation. Every detected link occurrence is stored separately from
  aggregated graph edges.
- Make provenance explicit. Each graph can be traced to a manifest hash, repository identity, exact
  Git revision, adapter version, and immutable source facts.
- Stay deterministic. Re-running the same manifest and repository revision must produce the same
  fact identities and graph metrics.
- Fail closed for untrusted input. Manifests, repository paths, tracked files, route policies, and
  build options are bounded and validated before storage.
- Keep the browser read-only. Dashboard and API routes can inspect stored snapshots; they cannot
  start ingestion, run builds, sync providers, or mutate source repositories.
- Prefer accessible bounded views over visually impressive but misleading full-graph hairballs.

## Engine layers

```text
manifest -> repository inspection -> source facts -> graph compilation -> report/display model
```

### Manifest

The YAML manifest declares the expected site identity and source boundary:

- site key and display name;
- repository root, expected origin, ref, and exact full revision;
- adapter mode such as `static-html`, `vinext`, or `auto`;
- route include/exclude policy;
- link-layer selectors and page-role selectors;
- goal routes used by distance and reachability metrics;
- limits for page count, file size, tree size, and source bytes.

The manifest is data, not code. It cannot provide a shell command. It rejects aliases, anchors,
explicit YAML tags, duplicate keys, unknown fields, secret-shaped field names, unsafe relative paths,
credentialed remotes, query strings, fragments, unsafe regexes, and unsupported build settings.

### Repository inspection

Inspection verifies that the repository matches the manifest before reading facts:

1. The repository root must be absolute and traversal-free.
2. The configured remote must normalize to the expected public origin without embedded credentials.
3. The selected ref must resolve to the exact expected full revision.
4. The worktree must be clean unless the manifest and operator explicitly allow a dirty snapshot.
5. Every tracked path is bounded and checked before file reads.

Inspection uses Git object commands against the selected revision. It does not check out, reset,
merge, push, build, install dependencies, or write to the inspected repository.

### Source facts

Source facts are the durable evidence layer. Page facts and link facts are stored with immutable
identities so replaying the same input is idempotent and changing content under an existing immutable
key fails.

Page facts include normalized route identity and source-level metadata needed for graph compilation.
Link facts preserve each occurrence, including:

- source route;
- destination route or unresolved destination;
- link layer;
- anchor sample;
- landmark sample;
- confidence;
- classification source;
- repeated-template and nofollow flags;
- structured evidence summary.

The browser read model does not expose raw source paths, repository local paths, remotes, account
references, or source excerpts.

## Adapters

### `static-html`

The static HTML adapter maps tracked HTML files to routes and parses anchor occurrences. Explicit
`data-link-layer` values take precedence, followed by semantic breadcrumbs, related-content markers,
CTA markers, navigation/footer/header landmarks, and a contextual fallback.

Use this adapter for exported static sites or simple HTML fixtures where route identity can be
derived directly from file paths.

### `vinext`

The vinext adapter reads tracked App Router and source files. It extracts route declarations,
internal links, navigation declarations, content-route declarations, and retired-route policy when a
supported public-pages declaration exists.

The adapter is source-first. It records bounded structural evidence from TypeScript, JavaScript, JSX,
TSX, and MDX files, but it does not claim that a route was deployed, crawled, visited, or converted.
Source-only classifications are labeled as bounded heuristics with confidence values.

The extractor searches common internal-link forms without executing project code:

- route-like object fields such as `slug`, `path`, `href`, `to`, `url`, `action`, and `formAction`;
- JSX attributes such as `href`, `to`, `action`, and `formAction`;
- simple string constants and one-level route maps referenced by JSX attributes;
- router-style calls such as `push`, `replace`, `prefetch`, `navigate`, `redirect`, and
  `permanentRedirect`;
- markdown and MDX inline links.

This is intentionally a static best-effort parser. Dynamic route construction, deeply nested route
objects, runtime feature flags, CMS data, and links created by external packages require either
explicit source literals or a future bounded build-output adapter before they can be treated as
complete evidence.

### `auto`

`auto` selects a supported adapter from the repository shape. It prefers vinext when App Router
signals are present, reports Astro detection, and otherwise falls back to static HTML where
appropriate. Unsupported shapes fail closed rather than guessing.

## Link layers

The engine keeps layer identity because not all links mean the same thing structurally.

| Layer | Meaning |
| --- | --- |
| `contextual` | In-body editorial or content links. |
| `related` | Related-content modules and recommended next reads. |
| `action` | Calls to action, contact paths, start paths, and conversion-oriented links. |
| `menu` | Primary or repeated navigation. |
| `breadcrumb` | Breadcrumb hierarchy. |
| `utility` | Footer, legal, accessibility, and secondary utility links. |

The default dashboard projection uses `contextual`, `related`, and `action`. Navigation layers can
be enabled explicitly. That default keeps the map focused on meaningful page-to-page pathways rather
than allowing repeated template navigation to dominate the visual.

## Graph compilation

Compilation reads the latest successful source snapshot for a site and creates a graph snapshot for a
projection. It aggregates link occurrences into unique edges by:

```text
source route + destination route + layer
```

Occurrences remain linked to their evidence even when a unique edge is emitted.

The compiler currently derives:

- page count;
- resolved and unresolved link counts;
- unique edge count;
- in-degree and out-degree;
- internal authority;
- shortest goal distance;
- unreachable pages;
- strongly connected components;
- true orphans across the complete internal topology;
- contextual orphans with inbound evidence only outside the selected layers;
- contextual dead ends;
- menu-dependent pages;
- homepage-dependent pages;
- global-shell-dependent pages;
- evidence-linked findings.

Component analysis is iterative so large acyclic sites do not hit Python recursion limits.

## Goal distance

Goal distance measures the shortest selected-layer path from a page to a configured goal route.

- `0` means the page is itself a goal.
- Positive values are hop counts.
- Negative or missing values mean the page is currently unreachable from the configured goals in the
  selected projection.

Goal distance is structural. It does not prove business value, conversion probability, or visitor
intent.

## Internal authority

Internal authority is a deterministic score derived from internal link topology. It helps identify
pages that are structurally emphasized by the site. It is not a search-engine ranking metric and
should not be presented as SEO authority outside the analyzed internal graph.

## Findings

Findings summarize graph conditions that are useful for review:

- true orphans with no complete-topology inbound evidence;
- contextual orphans with no selected-layer inbound evidence;
- contextual dead ends;
- menu-dependent pages;
- homepage- and global-shell-dependent pages;
- strongly connected component structure;
- unresolved internal references.

Every finding remains tied to stored evidence and selected projection settings.
Core 2.1 does not claim traps or bottlenecks: those labels require stronger algorithms and evidence
than the current compiler establishes.

## Report and display model

The report service builds one normalized read model for HTML, JSON, and CSV consumers. It includes:

- site and snapshot identity;
- selected layers;
- projection;
- coverage counts;
- complete Core 2.1 candidate, entity, relationship, and resolution-state totals;
- layer counts;
- graph-mode metadata;
- displayed and total node counts;
- displayed and total unique-edge counts;
- represented and total occurrence counts;
- truncation state and reasons;
- bounded visualization nodes and edges;
- complete edge table rows;
- page table;
- adjacency matrix;
- component summary;
- resilience view;
- entry-to-goal view;
- evidence rollup;
- snapshot diff when a previous snapshot is available.

The SVG view is intentionally bounded. It renders a coherent graph, not every stored relationship at
once. The complete table and CSV export are the accounting surfaces for full unique-edge coverage.
Reconciliation coverage is calculated from the complete persisted batch, never from displayed
nodes or edges. Corrected structural metrics are shown only when the selected display layers equal
the compiled contextual projection; otherwise they are explicitly withheld.

## SVG graph layout

The dashboard uses server-rendered SVG so it remains dependency-free and works with the existing
Content Security Policy.

The current layout is deterministic and organic:

1. Nodes are placed from graph topology, not source-folder or route-prefix buckets.
2. Connected components are separated first, with the selected page or goal component treated as the
   focus region.
3. Hop distance from the focus/goal creates intuitive role bands such as focus, entry page, one
   click away, two-click support, longer path, and disconnected in the current projection.
4. Edge springs pull related pages together while menu and utility edges exert weaker visual force
   than contextual, related, and action edges.
5. Collision passes account for node radius and estimated label width so circles do not stack on top
   of each other.
6. Stable hash-based jitter prevents grid-like placement while keeping refreshes repeatable.
7. Key pages stay labeled; secondary labels appear through hover, focus, and inspector behavior.
8. Curved directed paths preserve arrows while reducing visual overlap.
9. Menu, breadcrumb, and utility layers are visually quieter than contextual, related, and action
   layers.

The map includes plain-language guidance: circles are pages, arrows are internal links, and placement
follows link relationships plus click distance from the focus page. Exact route values remain
available in inspector, tables, JSON, and CSV even when the visual label uses a human-readable name.

## Human-readable names

The graph engine stores canonical routes. The dashboard derives display names from those routes for
readability. For example, `/services/website-migration-provider-rescue/` becomes
`Website Migration Provider Rescue`.

Acronyms such as SEO, B2B, AI, API, GA4, and D1 are preserved where recognized. The derived name is a
display convenience, not a source title. Exact canonical routes remain the durable identity.

## Bounds and completeness

The engine separates visual bounds from evidence completeness.

- SVG rendering is bounded by node and edge limits so the visual remains legible.
- The complete edge table is independent from the SVG bound.
- The CSV endpoint streams all matching resolved unique edges.
- All counts disclose displayed versus total values.
- Truncation reasons are explicit when a visual or neighborhood cap applies.

This prevents a bounded graph from being mistaken for a complete data set.

## Public/private boundary

Public source code and documentation may describe:

- manifest fields and validation behavior;
- adapters and graph metrics;
- generic loopback deployment guidance;
- placeholder CLI commands;
- security properties and failure modes.

Public source code and documentation must not contain:

- real local repository paths;
- private hostnames or IP addresses;
- SSH key paths;
- service unit names from a specific deployment;
- private coordination identifiers;
- tunnel identifiers;
- account-specific resource IDs;
- private manifest files;
- database backups;
- credentials, tokens, cookies, or auth headers;
- source excerpts from private client repositories.

## Extension points

Safe extension usually happens in one of four places:

1. Manifest schema: add a bounded declarative option and validation tests.
2. Adapter extraction: add a source-only parser that emits page and link facts.
3. Graph compilation: add a deterministic metric based on stored facts.
4. Report model: expose an aggregate that does not leak private source details.

Do not add a browser-triggered ingest path, dynamic repository checkout, shell command execution,
unbounded graph rendering, or raw evidence dumps to the web surface.

## Development checks

Run the focused and full checks before publishing a graph-engine change:

```console
python -m unittest discover -s tests -v
python scripts/verify_release.py
python -m build
python -B -m boho_analytics_platform --config examples/platform.demo.toml serve --help
git diff --check
```

For a release candidate, also inspect the committed diff for public-safety issues. Search for
organization-specific filesystem roots, hostnames, IP addresses, private coordination names, tunnel details,
private-key material, credential values, and real account identifiers. Matches in code that reject
secret-shaped fields or document placeholder credential names are expected. Matches containing real
operational values are release blockers.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Manifest validation fails | Unknown field, unsafe path, unsafe regex, or secret-shaped key | Simplify the manifest and keep credentials out of it. |
| Inspect rejects the repository | Origin/ref/revision mismatch or dirty worktree | Verify the expected Git revision and clean the checkout before ingestion. |
| Ingest succeeds but graph is sparse | Links are unresolved or filtered by selected layers | Check unresolved counts, route policy, and layer selection. |
| SVG omits some edges | Visual edge bound is active | Use the complete edge table or CSV export for full accounting. |
| Navigation overwhelms the graph | Menu/utility layers are selected | Start with contextual, related, and action layers, then enable navigation layers deliberately. |
| A page name looks too generic | Name is derived from route, not title metadata | Use exact routes in inspector/tables; add a title field to the page fact contract before treating titles as evidence. |
