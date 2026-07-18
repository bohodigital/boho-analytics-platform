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
presentation; the JSON and CSV values are identical across line, area, and bar views. Charts split
line and area paths at missing calendar dates instead of implying continuity.

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

`complete = true` requires every expected configured coverage cell. A missing provider row remains
missing unless an explicit zero fact or a successful binding-window query proves that quiet date.
Query-proven empty additive windows aggregate to zero without manufacturing stored metric facts.
Search Console coverage stops at its returned data-through date so normal provider latency is not
misstated as a finalized zero. Previous values, percentage changes, comparison charts, and comparison
CSV rows remain absent unless both periods have comparable complete coverage.

## Scale path

V1 queries indexed daily/hourly aggregates in SQLite. Retention is configurable. Backfills remain
bounded and restart-safe. Add report caching, queued large exports, rollups, or PostgreSQL only when
measured dataset size, write contention, or report latency exceeds the documented SQLite envelope.
