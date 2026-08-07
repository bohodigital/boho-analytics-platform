# Provider adapters

All adapters are read-only and emit cataloged aggregates. `probe` verifies credential usability;
`sync` collects one explicit window. Provider errors become sanitized categories in the ledger.

## Umami

V1 targets the current self-hosted API routes under `/api`. It supports API-key, bearer-token, and
username/password login credentials. Daily pageviews and daily unique visitors come from
`pageviews`; Umami names the second response array `sessions` but documents its values as visitors,
so the platform exposes it as `umami.daily-visitors` with non-additive-across-days semantics. The
adapter
requires an explicit pageview series, including an explicit empty series for a quiet window. It
queries and labels only exact whole-day windows in the configured site timezone. Exact-window
visitors, visits, bounces, and total time come from `stats`. All five documented stats fields are
required and the stats pageview total must reconcile exactly to the daily series. Exact-window
metrics are never summed across overlapping sync intervals.
Umami can serialize a non-UTC daily bucket as a timezone-less midnight label after applying the
requested timezone. The connector interprets only that midnight form in the explicit request
timezone; it does not relabel it as UTC or accept an arbitrary timezone-less time.
Platform windows are half-open; Umami's inclusive `endAt` is sent as one millisecond before the
exclusive boundary so adjacent days and 31-day headline chunks cannot count midnight twice.
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
bounded, paginated daily aggregate requests to `metrics/expanded`. One request per dimension type
parses Umami's returned `pageviews`, `visitors`, `visits`, `bounces`, and `totaltime` fields; it does
not send the unsupported `field` selector. `umami.route-pageviews` and `umami.route-visits` therefore
remain distinct measures from the same validated path row. Every request includes explicit `limit`
and `offset` values. A short page proves exhaustion only when every raw dimension identity is
unique across and within all pages. Repeated or overlapping identities stop pagination, discard the
overlapping page, retain prior privacy-safe rows as `UNKNOWN`, and never double-count. Reaching
`max_pages` with a full page retains only privacy-safe returned facts, marks them `UNKNOWN`, and
cannot establish complete route coverage or headline reconciliation. Browser, channel, country,
device, domain, event name, hostname, language, operating system, referrer, region, screen, tag, and
title aggregates remain individually disabled unless named in the binding; `["all"]` expands only
to that allowlist. Configured event series include explicit day grain, timezone, and event name and
aggregate repeated same-day rows. The connector never reads event payloads, event properties,
distinct IDs, raw sessions, IPs, user agents, city records, replay, or heatmaps. It checks
the provider-reported event extent before collection. A first observed calendar day that starts
after midnight is retained as `UNKNOWN`; it is not discarded or called complete. Pre-observation
days are not fabricated as zero. A request exceeding `max_days` or
`page_size` fails with a sanitized diagnostic. Provider contract:
<https://docs.umami.is/docs/api/website-stats>.

The requested window is intersected with the provider's observed event extent. A partially observed
first calendar day remains visible as `UNKNOWN`, while days before the first observation are absent;
neither is converted to a trustworthy quiet zero. The sync engine projects the requested calendar
dates onto each binding's configured site timezone, so one invocation can safely serve sites in
different zones while every provider still receives exact local-midnight boundaries.
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

V1 calls `sites/{siteUrl}/searchAnalytics/query` with an explicit search type, data state,
aggregation type, dimensions, row limit, and start row on every request. Date-only rows are control
totals. Every control row must contain clicks, impressions, CTR, and position; CTR is recomputed from
clicks and impressions, and position remains impression-weighted in reporting. The property string
must exactly match an accessible URL-prefix or domain property.
`search_types = ["all"]` executes web, image, video, News, Discover, and Google News independently;
search type stays in both fact identity and acquisition scope, so those surfaces are never blended.
Average position is emitted only for the four Google Search result surfaces (web, image, video, and
News). Discover and Google News omit position and do not expose query wording; the connector neither
turns that absence into zero nor requests query/page-query dimensions for those two surfaces.
Discover also omits the device grouping; the connector keeps page, country, appearance, and date
evidence but does not issue the unsupported Discover page/device combination.
Request dates use Search Console's `America/Los_Angeles` provider basis. Returned provider date
labels map into the same-named configured site reporting-day bucket. Every daily fact also retains
`provider_date` and `provider_timezone=America/Los_Angeles`, while the acquisition slice retains the
exact Pacific request interval; reports therefore do not silently present a site-local boundary as
Google's source boundary.
The headline request uses `dataState=all` and parses `first_incomplete_date`. Earlier rows are final;
that date and later rows are provisional. Settled high-dimensional requests stop before the marker,
and an incomplete empty snapshot cannot delete an older current fact. Once Google reports the day
settled, an authoritative empty response can retire it while immutable observation history remains.
Geographic search demand is collected per provider day with bounded `startRow` pagination. Search
Console does not expose a state or county dimension. Geography, page, query, page/query, device,
country, appearance, and cluster views remain `UNKNOWN` even after pagination reaches an empty page:
Search Analytics returns top rows, caps retrieval at roughly 50,000 rows per day and search type,
and withholds anonymized query text, so the API cannot prove a complete high-dimensional export.
When pagination bounds are omitted, the connector uses two 25,000-row result pages plus a third
terminal call, covering the API's documented 50,000-row daily/type/property ceiling. Reaching that
ceiling is still only API exhaustion, not proof that high-dimensional Google data itself is
complete.

Opt-in page observations add `date,page` requests with explicit `byPage` aggregation. Optional
device and country page breakdowns are disabled by default. Search appearance uses a discovery
request followed by one filtered page request per discovered appearance instead of combining the
appearance dimension with an incompatible aggregation. Named query clusters persist only their
configured cluster identifier and aggregates. Query wording is a separate explicit opt-in:
privacy-safe bounded text is stored with `query_visibility=safe`; unsafe text is aggregated into one
`[redacted]` bucket. Page/query capture requires that query opt-in. All high-dimensional views are
collected one provider day at a time to reduce top-row bias, and duplicate raw keys, a configured
page cap, or rows beyond the provider cap fail closed.
Pagination advances by the number of rows actually accepted, not by the requested page size, so a
short final result page can still request the exact 50,000-row terminal offset without skipping.

The optional hourly feed requests `dataState=hourly_all` for at most the provider's recent ten-day
window. It parses `first_incomplete_hour` and labels later hours provisional; request scope remains
part of every fact identity. The request slice stays provisional whenever Google supplies an
incomplete-hour marker, even if no row is returned after it. Schema-6 acquisition slices retain request dimensions, search type,
data state, aggregation, page and row counts, rejection counts, and the exhaustion reason while
immutable fact observations preserve revisions.

The Search Analytics API is not a substitute for Search Console bulk export. Complete settled
detail requires enabling the property's BigQuery bulk export; Google does not backfill dates before
activation. That export needs separate Google Cloud project/dataset IAM and is not unlocked by the
Search Console read-only OAuth scope.
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
