# Reporting model

The reporting system is designed around questions and reusable definitions, not fixed dashboard
screens. Custom time windows and focused sub-reports are first-class requirements.

## Report request

Every report request resolves to:

- Tenant/client and one or more sites.
- Absolute `start` and `end` instants plus a reporting timezone.
- Grain such as hour, day, week, or month.
- Completeness policy: realtime, provisional, final, or best available.
- Comparison window: previous period, previous year, or an explicit range.
- Metric identifiers from the catalog.
- Dimension filters and grouping.
- Named sections and an output format.

Relative windows such as `last_7_complete_days` are conveniences. They are converted to an absolute
window before querying so results can be reproduced later.

## Saved reports

A saved report is versioned configuration containing a title, permitted site scope, default window,
sections, metrics, groupings, filters, and display hints. It contains no SQL and no provider query.

The same definition can render HTML, JSON, CSV, or a print-oriented document. Presentation metadata
must not change metric calculation.

## Sub-reports

Sub-reports are normal report definitions with a narrower scope, such as:

- Organic-search landing pages.
- Conversion events by acquisition channel.
- Cache misses and server errors for one site.
- Content performance for a path prefix.
- Mobile search visibility for a custom date window.

A report bundle may link reports and sub-reports into a navigation tree. Calculations remain
independent and cycle-free; a sub-report does not inherit hidden SQL or mutate its parent definition.
Shared filters are explicit inputs.

## Large-history strategy

- Store normalized hourly and daily aggregates rather than every raw provider event.
- Keep recent fine-grained data for a configurable period and retain daily rollups longer.
- Backfill in bounded chunks and checkpoint progress.
- Cache report results by tenant scope, definition version, absolute window, filters, and data
  watermark.
- Run expensive exports as queued jobs only after synchronous query latency proves insufficient.
- Add PostgreSQL partitioning only after measurements justify the migration.

Arbitrary custom windows are computed from retained facts and rollups. The platform does not
precompute every possible report window.

## Metric integrity

Each output displays its provider, definition, window, timezone, last successful sync, completeness,
and important provider limitations. Metrics with incompatible meanings are shown side-by-side rather
than merged.

Derived metrics declare their formula, units, input sources, null behavior, and version. Changing a
formula creates a new definition version so historical reports remain explainable.
