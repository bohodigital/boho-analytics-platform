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
presentation; the JSON and CSV values are identical across line, area, and bar views. The series CSV
uses stable columns: `period,date,metric,site_id,source,unit,value`.

## Output contract

Report JSON includes schema version, report/subreport/site scope, applied filters, resolved current and
comparison windows, generation time, rows, compact current and comparison daily series, per-source observation freshness,
forms-pipeline reconciliation, warnings, and completeness. Each row includes metric, site, source,
unit, current value, prior value, and percentage change. Series preserve provider labels and include
only returned dates; the dashboard does not invent zeroes for omitted dates.

Report CSV contains the flat aggregate-row columns. Series CSV contains one daily value per row and
labels current versus comparison periods. Both deliberately exclude credentials, resource IDs,
configuration, form/message content, and provider payloads.

## Semantics

Metric catalog rules determine aggregation. Additive counts/bytes sum; Search Console CTR and
position are weighted; latest-state metrics select the newest point; exact-window unique/summary
metrics never sum overlapping sync windows. Cross-provider visitor measures remain separate.

`complete = false` and a warning mean at least one configured metric has no stored contribution in
the requested window. Zero is emitted only when a provider explicitly returned zero.

## Scale path

V1 queries indexed daily/hourly aggregates in SQLite. Retention is configurable. Backfills remain
bounded and restart-safe. Add report caching, queued large exports, rollups, or PostgreSQL only when
measured dataset size, write contention, or report latency exceeds the documented SQLite envelope.
