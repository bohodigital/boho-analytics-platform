# Production web application roadmap

Status: implementation plan. This document does not authorize a production deployment, DNS change, account change, or credential change.

## Product boundary

The production application is a read-only analytics workspace for the Boho portfolio. It presents stored evidence from provider feeds; browser requests do not collect provider data, run crawls, compile site graphs, or receive provider credentials.

The initial hosted release is an owner-only preview. Multi-user access follows only after property-scoped authorization and cross-tenant isolation tests pass.

## Route map

- `/`: portfolio or single-property operating summary.
- `/?view=plot`: precise chart builder for stored metrics.
- `/site-graph`: link structure, reachability, and structural findings.
- `/route-observations`: content and acquisition explorer with bounded raw evidence.
- `/api/v1/*`: read-only JSON and CSV interfaces governed by the same identity, role, property, and export rules as the HTML application.

Every HTML route uses one application header, navigation model, theme contract, focus treatment, typography scale, metric formatter, and data-state vocabulary.

## Themes

The first production theme set is intentionally dark and OLED-safe:

- Hyperpunk: cyan and magenta signals.
- Ultraviolet: violet and teal signals.
- Ember: orange and acid-green signals.

Themes use semantic tokens, not route-specific color substitutions. Tokens apply to HTML, canvas charts, maps, site-graph SVG, tables, forms, tooltips, focus indicators, warnings, and positive/negative states. A theme may change presentation but never the meaning assigned to a state or series.

The browser stores only the selected theme locally. User-profile persistence can replace this after application accounts exist.

## Hosted architecture

```text
Browser
  -> HTTPS hostname on Cloudflare
  -> Cloudflare Access identity and allow policy
  -> Cloudflare Tunnel with Access-token validation
  -> loopback-bound Boho Analytics service on the Pi
  -> read-only analytics database on the Seagate volume
```

The Pi application port remains private. Cloudflare Access authenticates the person, while the application authorizes the resulting identity for roles and properties. Cloudflare policy alone is not the tenant boundary.

Required application roles:

- `owner`: all configured properties, exports, and operational diagnostics.
- `analyst`: assigned properties, reports, and bounded exports.
- `viewer`: assigned properties and reports; no bulk export by default.

Every report and API query must resolve an authenticated principal before resolving property scope. Requested properties are intersected with the principal's assignments. Empty, unknown, or unauthorized scopes fail closed. Tests must prove that URL, form, CSV, and API manipulation cannot cross property assignments.

## Release stages

### 1. Interface foundation

- Shared application shell and navigation on every route.
- Semantic theme tokens and persistent theme selector.
- Summary-first Plot, Site Graph, and Routes pages.
- Raw evidence and advanced controls remain available but collapsed by default.
- Responsive, keyboard, contrast, and chart-tooltip QA.

### 2. Authorization boundary

- Trusted identity adapter for Cloudflare Access JWT claims.
- Issuer, audience, expiry, and signature validation with rotating keys.
- Role and property assignments stored outside the repository.
- Property-scoped queries and exports enforced below route rendering.
- Structured audit events for login identity, denied scope, export, and administrative changes without logging sensitive query strings.

### 3. Owner-only hosted preview

- Dedicated application subdomain.
- Cloudflare Access application created before the tunnel route.
- Tunnel performs Access-token validation before proxying to the origin.
- Origin remains loopback-bound and rejects requests without a validated identity context.
- HTTPS, CSP, secure headers, request limits, export limits, backup verification, health checks, and rollback are exercised.

### 4. Controlled production

- Owner accepts a named release artifact and rollback target.
- DNS and Cloudflare changes receive explicit approval.
- Viewer/analyst users are added only after property-isolation tests pass.
- Operational runbook covers user removal, token/key failure, tunnel failure, database restore, and incident response.

## Production acceptance gates

- All automated tests pass from a clean release tree.
- The Pi checkout, release artifact, health identity, and declared Git commit are reconcilable.
- No provider credential, database, log, export, or runtime file is in Git or the web root.
- Counts use integer axes and exact hover values; rates, durations, bytes, and positions retain their correct units.
- Every visible total states its source, window, scope, and completeness or links directly to that definition.
- All four HTML routes work at desktop and mobile widths with no overlap or horizontal page overflow.
- Theme changes update all visual primitives and preserve readable contrast and non-color state cues.
- Authentication fails closed; authorization is property-scoped; HTML, JSON, and CSV have equivalent enforcement.
- Origin bypass, unauthorized property access, over-broad export, stale identity keys, and logout/revocation are tested.
- A rollback release and database backup are verified before production traffic changes.

## Decisions still requiring the owner

- Production hostname.
- Identity provider allowed by Cloudflare Access.
- Initial owner identities and session duration.
- Whether the first release is owner-only or includes additional viewers.
- Which roles may export CSV and the maximum export size.

