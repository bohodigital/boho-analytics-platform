# Rendered crawl security boundary

The Core 2.1 rendered adapter is an optional, explicitly scoped evidence collector. It does not
bundle, launch, or choose a browser. A reviewed caller must inject a browser implementation that
creates a new context from the adapter-provided temporary profile and applies `RequestPolicy`
before every navigation, redirect, subresource, fetch, and service-worker request.

## Authorization and provenance

Every run requires one exact HTTP(S) target origin, an exact expected revision, and an exact
observed revision. Origins may not contain credentials, paths, queries, or fragments. A revision
mismatch opens no browser and records every otherwise valid route as `unchecked` with
`contradicted` revision evidence. The adapter never infers a host, follows a host-changing redirect,
or silently associates evidence with a different revision.

Additional origins are an explicit, bounded static-origin allowlist. They may serve only scripts,
stylesheets, images, or fonts. They may not receive top-level navigation, fetch/XHR, document,
websocket, or form traffic.

## Network and interaction policy

Only GET and HEAD are allowed. The request policy blocks:

- non-authorized origins and origin-changing redirects;
- analytics, advertising, tracker, telemetry, pixel, and beacon targets;
- form and state-changing methods;
- mail and telephone schemes;
- purchase, checkout, booking, account, authentication, login/logout, save/delete, subscription,
  consent, and similar action targets.

The adapter never clicks. Forms and action-looking anchors are recorded as `action` evidence, not
page topology. Fragment links are `fragment`; off-origin links are `external`. Menus, tabs,
accordions, disclosure widgets, and other interaction recipes are unsupported.

## Disposable state and cleanup

Desktop and mobile captures each receive a fresh temporary profile and browser context. On success,
failure, timeout, or cancellation, the adapter clears browser state and closes the context in a
`finally` path; temporary-profile cleanup then runs. The injected browser must implement
`clear_state()` to remove cookies, local and session storage, caches, service workers, permissions,
and credentials, and `close()` must terminate child processes. A wrapper that cannot meet that
contract is not admissible.

## Bounded and privacy-safe evidence

The adapter bounds routes, origins, link occurrences, DOM bytes, diagnostics, strings, and timeout.
It stores a DOM hash, never raw DOM. Console and network evidence are fixed failure classes, not raw
messages or request bodies. Credential-bearing URLs are discarded. Query values are removed from
captured URLs, anchor and form targets; crawl inputs containing queries are left `unchecked`; and
mail/tel destinations are redacted. Cookies, headers, storage,
credentials, screenshots, request bodies, response bodies, and browser profiles are never emitted.

Timeouts, navigation failures, partial hydration, oversized DOM, blocked resources, invalid routes,
and unavailable evidence stay explicit in coverage. Missing evidence is never interpreted as page
absence.

## Current implementation boundary

The repository ships only the policy, adapter, deterministic replay CLI, and injected protocols. It
has no browser dependency and performs no live-site crawl. Any browser wrapper requires its own
dependency audit and must prove request interception occurs before all requests, including redirects
and service workers.
