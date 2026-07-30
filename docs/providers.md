# Provider adapters

All adapters are read-only and emit cataloged aggregates. `probe` verifies credential usability;
`sync` collects one explicit window. Provider errors become sanitized categories in the ledger.

## Umami

V1 targets the current self-hosted API routes under `/api`. It supports API-key, bearer-token, and
username/password login credentials. Daily pageviews/sessions come from `pageviews`; the adapter
requires an explicit pageview series, including an explicit empty series for a quiet window. It
queries and labels only exact whole-day windows in the configured site timezone. Exact-window
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
bounded, paginated daily aggregate requests to `metrics/expanded`. `umami.route-pageviews` is a
distinct metric fetched with `type=path&field=pageviews`; `umami.route-visits` is fetched separately
with `type=path&field=visits` and is never relabeled or substituted. Every request includes explicit
`limit` and `offset` values. A short page proves exhaustion only when every raw `name` identity is
unique across and within all pages. Repeated or overlapping identities stop pagination, discard the
overlapping page, retain prior privacy-safe rows as `UNKNOWN`, and never double-count. Reaching
`max_pages` with a full page retains only privacy-safe returned facts, marks them `UNKNOWN`, and
cannot establish complete route coverage or headline reconciliation. Title, channel, domain,
device, country, and configured event
facts remain individually disabled unless named in the binding. It never reads event payloads,
event properties, distinct IDs, sessions, IPs, user agents, or city records. The connector checks
the provider-reported available date range before collection and rejects a request that predates it
rather than treating pre-instrumentation silence as zero. A request exceeding `max_days` or
`page_size` fails with a sanitized diagnostic. Provider contract:
<https://docs.umami.is/docs/api/website-stats>.

The exact requested half-open interval must be contained in the provider's availability timestamps;
matching only the site-local start and end dates is insufficient. The sync engine projects the
requested calendar dates onto each binding's configured site timezone, so one invocation can safely
serve sites in different zones while every provider still receives exact local-midnight boundaries.
Reporting projects the same requested calendar dates per site for fact queries, coverage cells,
series labels, and provider comparison. Headline and route pageviews accept only non-negative
integral counts. Invalid headline pageviews
fail the sync, while an invalid route row is omitted and makes retained privacy-safe rows `UNKNOWN`.

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
sessions, pageviews, event count, and key events. The adapter requires the `screenPageViews`
series explicitly, including its header on a valid empty response. Google API end dates are
inclusive, so the adapter converts the exact whole-day request to the configured site timezone and
subtracts one day from the platform's exclusive local end. Returned headline rows must have one
value for every declared metric and a date inside that interval; malformed or out-of-window rows
fail the acquisition.

Credentials may be a short-lived access token, OAuth refresh-token fields, or service-account JSON.
The GA4 property must grant the chosen identity sufficient viewer access.
Probe executes a minimal report against every configured property. A reported property timezone
must match the bound site timezone; a mismatch fails closed, while omitted timezone metadata remains
an explicit warning rather than an assumption.
The geography query groups sessions by date, ISO country ID, and provider-reported region. Country
and region values remain GA4 sessions and are not deduplicated against Umami or Cloudflare.

Opt-in route observations use GA4 `runReport` pagination and property metadata validation. They
collect landing-page sessions, engagement, and key events. `google.page-path-views` uses the
`pagePath` dimension and `screenPageViews` metric. Its dimension must be an internal normalized
pathname with no query or fragment. Pagination returns complete facts only when provider exhaustion
is proven; a configured page cap retains safe rows as `UNKNOWN` rather than claiming complete
coverage. Every page, including an empty page, must return dimension header names exactly matching
`date` plus the requested dimension and one metric header name exactly matching the requested
metric. Every row must then have exactly two dimension values and one bounded metric value.
`rowCount` must be present, bounded, and consistent on every page; neither a short page nor a count
remembered from an earlier page can replace missing metadata. Repeated raw dimension identities
across pages also fail exhaustion closed. Returned route dates must fall inside the requested
calendar window. Pageview values must be non-negative integral counts, and rejected
dates, counts, or privacy dimensions downgrade retained safe rows to `UNKNOWN`. Title, channel, and
referrer families remain off unless individually selected in
`ga4_dimensions`; event counts require individually configured names. Internal referrers retain a
normalized route; external referrers retain an allowlisted domain only. Full referrer URLs, event
parameters, client IDs, session IDs, and raw events are never facts or diagnostics. Contracts:
<https://developers.google.com/analytics/devguides/reporting/data/v1/basics> and
<https://developers.google.com/analytics/devguides/reporting/data/v1>.

GA4 and Umami headline and route facts remain provider-labeled. Reporting compares pageviews only
on mature calendar dates with complete evidence from both providers. After this acquisition-contract
upgrade, a fresh successful GA4 and Umami sync is mandatory before provider pageview coverage or
quiet zeroes can become authoritative. Fresh runs carry an explicit-pageviews marker in the existing
ledger result-kind field; legacy `data`/`empty` rows remain untouched and fail closed, with no schema
bump or migration. Retained facts are revalidated as finite, non-negative, integral, bounded counts
at report time. Series and plot requests still enforce explicit-run acquisition authorization for
requested native headline facts, but skip provider comparison construction and route-fact
materialization; dedicated reports retain comparison behavior. Session-, token-, reset-, and
resource-labelled opaque identities and encoded path separators are rejected by the route privacy
boundary without entering facts or diagnostics. A conservative vocabulary permits clearly lexical
lowercase hyphenated content slugs such as `appointment-booking` and `article-alpha`;
arbitrary base64url-shaped hyphenated strings remain rejected.
Provider headline and route dates, including Search Console route dates, outside their
requested provider interval are rejected before fact construction. See
[ADR 0005](adr/0005-provider-pageview-comparability.md) for the comparison and reconciliation
decision.

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
