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

The web form and `/api/v1/report` use the same calculation. `/api/v1/report.csv` exports the same row
set. Browser requests never initiate syncs.

## Output contract

JSON includes schema version, report/subreport IDs, applied filters, resolved current and comparison
windows, generation time, rows, per-source observation freshness, forms-pipeline reconciliation,
warnings, and completeness. Each row includes metric, site, source, unit, current value, prior value,
and percentage change.

CSV contains the flat row columns. It deliberately excludes credentials, resource IDs, configuration,
form/message content, and provider payloads.

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
