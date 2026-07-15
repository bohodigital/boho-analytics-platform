# Data model

The logical model separates configuration, provider capability, ingestion state, normalized facts,
and reporting definitions.

## Configuration records

- Tenant/client
- Site
- Provider connection
- Provider resource binding

## Capability records

A capability snapshot records connection, provider, probe time, authentication result, discovered
resources, supported metric groups, available history, quota hints, and sanitized warnings. The UI
uses this record to explain missing panels.

## Ingestion records

A sync run records connector version, bounded window, attempt, outcome, row counts, retry category,
data watermark, and sanitized error code. It never stores credentials or private response bodies.

## Metric facts

A metric point includes:

- Site and provider source.
- Stable metric identifier and unit.
- Start, end, and time grain.
- Numeric value.
- Canonical dimension pairs.
- Completeness state.
- Observation and provider-update timestamps.

The natural upsert key is site, source, metric, interval, grain, and canonical dimensions.

## Report definitions and cache

Report definitions are versioned, non-executable data. Cached results include the definition version,
resolved absolute window, filters, tenant scope, and highest contributing data watermark. This makes
cache invalidation deterministic and reports reproducible.

## Privacy defaults

The normalized store does not require IP addresses, user identifiers, session identifiers, or raw
query strings from page URLs. Search terms and custom event properties can still be commercially or
personally sensitive, so deployments must apply tenant authorization and retention controls.
