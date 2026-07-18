# Provider adapters

All adapters are read-only and emit cataloged aggregates. `probe` verifies credential usability;
`sync` collects one explicit window. Provider errors become sanitized categories in the ledger.

## Umami

V1 targets the current self-hosted API routes under `/api`. It supports API-key, bearer-token, and
username/password login credentials. Daily pageviews/sessions come from `pageviews`; exact-window
visitors, visits, bounces, and total time come from `stats`. Exact-window metrics are never summed
across overlapping sync intervals.

Umami Cloud and differently versioned self-hosted installations may use different base paths or
authentication behavior. Confirm live version compatibility during connection testing.

## Cloudflare traffic

V1 uses the GraphQL Analytics endpoint and `httpRequestsAdaptiveGroups` grouped by date with
`requestSource: eyeball`. It stores returned estimated requests, visits, and edge-response bytes
without multiplying sampling intervals. Those facts are provisional and reports disclose adaptive
sampling. Probe runs the configured zone query rather than validating the token alone. A dedicated
read-only Analytics token is preferred.

## Google Analytics

V1 calls GA4 Data API `properties/{property}:runReport` with a date dimension and daily active users,
sessions, pageviews, event count, and key events. Google API end dates are inclusive, so the adapter
subtracts one day from the platform's exclusive report end.

Credentials may be a short-lived access token, OAuth refresh-token fields, or service-account JSON.
The GA4 property must grant the chosen identity sufficient viewer access.
Probe executes a minimal report against every configured property. A reported property timezone
must match the bound site timezone; a mismatch fails closed, while omitted timezone metadata remains
an explicit warning rather than an assumption.

## Google Search Console

V1 calls `sites/{siteUrl}/searchAnalytics/query` with daily rows, final data, and the documented
25,000-row ceiling. Daily-only windows fit under that ceiling, but Search Console returns top rows
rather than guaranteed exhaustive high-dimensional data. CTR is recomputed and position is weighted
by impressions in reports. The property string must exactly match an accessible URL-prefix or domain
property.
Request dates use Search Console's `America/Los_Angeles` provider basis. Returned provider date
labels map into the configured site-day storage bucket to preserve the existing fact identity and
reporting contract; the provider basis remains explicit in health and probe metadata.

## Cloudflare forms D1

The adapter calls the D1 query API with one fixed parameterized aggregate `SELECT`. It groups date,
form ID, and notification status at the provider and never retrieves submission payloads. See
[`forms-monitoring.md`](forms-monitoring.md).
Window bounds are converted to UTC instants for filtering, then aware provider timestamps are
grouped into the configured site timezone. Missing, naive, or invalid timestamps fail instead of
being silently assigned to a day. Multiple provider rows for the same local day, form, and status are
summed before storage. The corrected day identity is versioned so legacy UTC-date aggregates remain
available as lineage without being included in current reports. Probe executes a resource-specific
aggregate query.

## Forms inbox

The adapter is local, not an external API. It opens the existing comms-platform SQLite index in
read-only/query-only mode, applies configured mailbox/sender/subject filters, and emits daily delivery
and unread counts, including explicit zero days. Stable daily facts keep overlapping sync windows
idempotent. It never owns synchronization or mailbox state.
It filters by UTC instant, groups into the configured site timezone, and verifies each configured
mailbox during probe.

## Fixture

The fixture connector is deterministic and needs no credential. Fixture keys resembling form/mail
content, credentials, IPs, or user agents are rejected recursively. Use it for CI, demonstrations,
report design, and failure testing.
