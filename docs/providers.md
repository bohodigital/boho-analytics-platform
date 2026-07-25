# Provider adapters

All adapters are read-only and emit cataloged aggregates. `probe` verifies credential usability;
`sync` collects one explicit window. Provider errors become sanitized categories in the ledger.

## Umami

V1 targets the current self-hosted API routes under `/api`. It supports API-key, bearer-token, and
username/password login credentials. Daily pageviews/sessions come from `pageviews`; exact-window
visitors, visits, bounces, and total time come from `stats`. Exact-window metrics are never summed
across overlapping sync intervals.
Country and region visit aggregates come from `metrics/expanded`. They keep the exact requested
window identity, store only ISO codes and aggregate values, and never store IP addresses, visitor
IDs, session IDs, coordinates, cities, or raw provider responses.

Set each Umami binding's `observation_start` to the first site-local date on which the tracker and
website record are independently verified. This prevents successful queries and legacy facts from
turning pre-instrumentation dates into authoritative zeroes. Reports crossing that boundary remain
partial, and exact-window totals spanning it cannot become complete decision inputs.

Umami Cloud and differently versioned self-hosted installations may use different base paths or
authentication behavior. Confirm live version compatibility during connection testing.

Route observations are disabled unless a binding explicitly opts in. The connector then issues
bounded, paginated daily aggregate requests for paths, entries, and exits. Title, channel, domain,
device, country, and configured event facts remain individually disabled unless named in the
binding. It never reads event payloads, event properties, distinct IDs, sessions, IPs, user agents,
or city records. The connector checks the provider-reported available date range before collection
and rejects a request that predates it rather than treating pre-instrumentation silence as zero. A
request exceeding `max_days`, `max_pages`, or `page_size` fails with a sanitized diagnostic rather
than silently collecting a partial route slice. Provider contract:
<https://docs.umami.is/docs/api/website-stats>.

## Cloudflare traffic

V1 uses the GraphQL Analytics endpoint and `httpRequestsAdaptiveGroups` grouped by date with
`requestSource: eyeball`. It stores returned estimated requests, visits, and edge-response bytes
without multiplying sampling intervals. Those facts are provisional and reports disclose adaptive
sampling. Probe runs the configured zone query rather than validating the token alone. A dedicated
read-only Analytics token is preferred. Probe also discovers each zone's plan-bound `maxDuration`
and `notOlderThan` limits and reports the conservative minimum across configured zones.
The geography query groups the same provisional visit measure by date and country. It is a separate
provider-labeled layer and is never blended with browser-analytics visits.

## Google Analytics

V1 calls GA4 Data API `properties/{property}:runReport` with a date dimension and daily active users,
sessions, pageviews, event count, and key events. Google API end dates are inclusive, so the adapter
subtracts one day from the platform's exclusive report end.

Credentials may be a short-lived access token, OAuth refresh-token fields, or service-account JSON.
The GA4 property must grant the chosen identity sufficient viewer access.
Probe executes a minimal report against every configured property. A reported property timezone
must match the bound site timezone; a mismatch fails closed, while omitted timezone metadata remains
an explicit warning rather than an assumption.
The geography query groups sessions by date, ISO country ID, and provider-reported region. Country
and region values remain GA4 sessions and are not deduplicated against Umami or Cloudflare.

Opt-in route observations use GA4 `runReport` pagination and property metadata validation. They
collect landing-page sessions, page-path views, engagement, and key events. Title, channel, and
referrer families remain off unless individually selected in `ga4_dimensions`; event counts require
individually configured names. Internal referrers retain a normalized route; external referrers
retain an allowlisted domain only. Full referrer URLs, event parameters, client IDs, session IDs,
and raw events are never facts or diagnostics. Contracts:
<https://developers.google.com/analytics/devguides/reporting/data/v1/basics> and
<https://developers.google.com/analytics/devguides/reporting/data/v1>.

## Google Search Console

V1 calls `sites/{siteUrl}/searchAnalytics/query` with daily rows, final data, and the documented
25,000-row ceiling. Daily-only windows fit under that ceiling, but Search Console returns top rows
rather than guaranteed exhaustive high-dimensional data. CTR is recomputed and position is weighted
by impressions in reports. The property string must exactly match an accessible URL-prefix or domain
property.
Request dates use Search Console's `America/Los_Angeles` provider basis. Returned provider date
labels map into the configured site-day storage bucket to preserve the existing fact identity and
reporting contract; the provider basis remains explicit in health and probe metadata.
Geographic search demand is a second query grouped by provider date and ISO alpha-3 country. Search
Console does not expose a state or county dimension, and high-dimensional results are top rows rather
than guaranteed exhaustive data.

Opt-in page observations add `date,page` Search Analytics requests with `startRow` pagination,
`aggregationType=auto`, the configured search type, and final data only. Existing date-only facts
remain control totals; page rows are provider-limited and stored with unknown completeness. Optional
device, country, and search-appearance dimensions are disabled by default. Named query clusters
persist only their configured cluster identifier and aggregate values, never a returned query value.
Every fact also records `data_state=final` and an explicit observation scope (`page`, one named
page-breakdown scope, or `query-cluster`) so overlapping views cannot be mistaken for one additive
population.
Contract: <https://developers.google.com/webmaster-tools/v1/searchanalytics/query>.

## Geographic display boundary

Geographic facts use dedicated metric IDs so dimension rows cannot inflate the existing headline
totals. The read-only `/api/v1/geography` endpoint suppresses buckets below three observations and
never reports an omitted bucket as zero or complete. Umami is the primary country/state view; GA4
corroborates configured sites, Search Console represents search clicks, and Cloudflare represents
adaptive edge visits. None is treated as person-level or household-level location.

Natural Earth v5.1.2 country boundaries and US Atlas v3.0.1 Census-derived state/county boundaries
are packaged and served locally. County geometry supports visual drilldown only. The system does not
infer county values from city names, postal codes, coordinates, or IP addresses.

## Cloudflare forms D1

The adapter calls the D1 query API with one fixed parameterized aggregate `SELECT`. It groups date,
form ID, and notification status at the provider and never retrieves submission payloads. See
[`forms-monitoring.md`](forms-monitoring.md).
Window bounds are converted to UTC instants for filtering, then aware provider timestamps are
grouped into the configured site timezone. Missing, naive, or invalid timestamps fail instead of
being silently assigned to a day. Multiple provider rows for the same local day, form, and status are
summed before storage. The corrected day identity is versioned so legacy UTC-date aggregates remain
available as lineage without being included in current reports. Probe executes a resource-specific
aggregate query and reports the explicitly configured D1 source retention. The connector requires
`source_retention_days`, rejects values above the verified 90-day ceiling, and fails closed unless a
sync request contains only completed site-local days strictly newer than the retention cutoff day.

## Forms inbox

The adapter is local, not an external API. It opens the existing comms-platform SQLite index in
read-only/query-only mode, applies configured mailbox/sender/subject filters, and emits daily delivery
and unread counts. It counts distinct message identities, may exclude configured synthetic subject
markers, and emits quiet-day zeroes only from a configured trustworthy `observation_start`. Without
that boundary, only actual matching-message days are facts. Stable daily facts keep overlapping sync
windows idempotent. It never owns synchronization or mailbox state.
It filters by UTC instant, groups into the configured site timezone, and verifies each configured
mailbox during probe.

## Fixture

The fixture connector is deterministic and needs no credential. Fixture keys resembling form/mail
content, credentials, IPs, or user agents are rejected recursively. Use it for CI, demonstrations,
report design, and failure testing.
