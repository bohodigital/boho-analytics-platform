# Reporting model

V1 reports are saved, non-executable TOML definitions. Each specifies client scope, sites, metric
IDs, and a default window. Subreports narrow metrics and may add exact canonical-dimension filters,
such as `form_id = "contact"`.

## Windows and comparisons

CLI and API requests resolve to an inclusive start and exclusive end at local midnight in the
configured reporting timezone. `--days N` ends at the start of today, so it represents complete
days. The previous comparison uses an immediately preceding window of identical duration.

Absolute windows make results reproducible:

```bash
boho-analytics --config /private/platform.toml report summary \
  --start 2026-04-01 --end 2026-07-01 --format json
```

The web form and `/api/v1/report` use the same calculation. An optional `site` parameter narrows a
saved report to one of its configured sites. `/api/v1/report.csv` downloads the same row set. Browser
requests never initiate syncs.

The Plot Builder uses `/api/v1/series` and `/api/v1/series.csv`. It accepts the same report, site,
start, and end scope plus `source`, `metric`, `style`, and optional `compare=1`. Style affects only
presentation; the JSON and CSV values are identical across supported line, area, and bar views.
Lower-is-better metrics are line-only so area or bar magnitude cannot encode desirability instead
of the stated value. Their axes remain conventional numeric axes and the direction is disclosed in
text. Charts split line and area paths at missing calendar dates instead of implying continuity.

## Output contract

Report JSON schema version 2 includes report/subreport/site scope, applied filters, resolved current
and comparison windows, generation time, rows, compact series, `summary_totals`, `coverage`,
`source_health`, comparison status, nullable forms-pipeline reconciliation, warnings, and strict
completeness. Coverage is counted per configured site, source, metric input, and expected date (or
one exact-window cell for window metrics). Intentionally unconfigured site/source combinations have
zero expected cells and do not degrade configured metrics. Missing detail is represented as compact
consecutive ranges rather than one object per absent day. Source health separates provider data-through from local
ingestion time and discloses time-basis, sampling, and data-state assumptions.

Report CSV contains flat aggregate rows. Series CSV contains one daily value per row and labels
current versus comparison periods. Both append report/window/timezone, aggregation, coverage,
comparison availability, data-through, ingestion, time-basis, sampling, and data-state context.
They deliberately exclude credentials, resource IDs, configuration, form/message content, and raw
provider payloads.

## Semantics

Metric catalog rules determine aggregation. Additive counts/bytes sum; Search Console CTR and
position are weighted; exact-window unique/summary
metrics never sum overlapping sync windows. Cross-provider visitor measures remain separate.

Umami's daily visitor series is a daily-unique measure. It may be plotted one day at a time, but it
is never added into a report-window visitor total. The separate `umami.visitors` metric is the
provider's unique count for one exact requested window. Likewise, GA4 daily active users may be
summed only when the display explicitly calls the result **active-user days**; it is not a unique
visitor count for the whole window.

Search Console facts are scoped to one search surface (`web`, `image`, `video`, `news`, `discover`,
or `googleNews`). A report selects and discloses one surface before aggregation. Surfaces are never
silently added, averaged, or substituted for one another. Search Analytics dimension rows are
provider-visible top rows rather than an exhaustive export, even when the platform exhausts its
configured pagination plan.

`complete = true` requires every expected configured coverage cell. A missing provider row remains
missing unless an explicit zero fact or a successful binding-window query proves that quiet date.
Query-proven empty additive windows aggregate to zero without manufacturing stored metric facts.
Search Console coverage stops at its returned data-through date so normal provider latency is not
misstated as a finalized zero. Previous values, percentage changes, comparison charts, and comparison
CSV rows remain absent unless both periods have comparable complete coverage.

## Display contract

The overview is an operating view, not a composite score. Every headline number identifies its
provider, measurement window, aggregation meaning, and coverage state. Report-cell coverage is
shown as covered versus expected cells; it is not labeled confidence, accuracy, or trust. Partial
additive totals may be displayed only as **observed totals** with their covered/expected cell count.
Weighted or non-additive aggregates remain withheld when their required inputs are incomplete.
Headline totals also disclose contributing and configured site counts so a metric configured for
only part of a portfolio cannot imply portfolio-wide measurement.

Missing, withheld, not configured, provisional, redacted, and true zero are different states and
must remain visibly different in HTML, JSON, CSV, and chart fallbacks. Period changes are shown only
for equal-length windows with comparable complete coverage. A valid zero prior period is labeled
`New` when the current value is non-zero and `No change` when both values are zero; it is never
reported as missing prior data.

Provider reconciliation stays supporting evidence. GA4 sessions, Umami visits, Search Console
clicks, Cloudflare edge estimates, and durable form submissions answer different questions and are
never merged into a synthetic traffic or attention total.

Geography titles, metric/grain labels, privacy suppression, methodology, regional support, county
limits, and accessible map labels all come from the currently selected provider payload. Switching
map sources clears the prior payload before rendering the next one, so Umami, GA4, Search Console,
and Cloudflare claims cannot remain attached to one another.

## Scale path

V1 queries indexed daily/hourly aggregates in SQLite. Retention is configurable. Backfills remain
bounded and restart-safe. Add report caching, queued large exports, rollups, or PostgreSQL only when
measured dataset size, write contention, or report latency exceeds the documented SQLite envelope.
