# Privacy boundary

The SQLite acquisition and browser-reporting plane collects only bounded aggregate facts. Route
observations are disabled by default and must be explicitly enabled per binding.

That plane never persists raw sessions, visitor/client/distinct IDs, IP addresses, user agents,
city-level locations, form payloads, emails, phone numbers, unscreened Search Console queries,
arbitrary event parameters, unrestricted query strings, or full external referrer URLs. Internal
URLs pass a shared canonical route normalizer; external referrers are discarded unless their
hostname is on the binding allowlist, in which case only that domain is stored.

Configured Search Console query clusters are acquisition filters only. Stored facts contain the
configured cluster ID and aggregates, never the provider query dimension or returned query text.
Separately, `search_console_query_text` is an explicit opt-in for wording analysis. Query values are
bounded and screened for direct contact data and URLs before storage; rejected values contribute to
one `[redacted]` aggregate with `query_visibility=redacted`, preserving counts without retaining the
text. Google-anonymized queries do not exist in the API response and therefore cannot be recovered.
Provider diagnostics expose bounded categories and limits, not request or response bodies.

Search Console BigQuery bulk export is an explicit exception to the SQLite storage contract. Its
complete aggregate query and URL dimensions are retained only as private immutable Parquet beneath
a UUID-verified external-filesystem root. Bulk rows never enter SQLite, dashboard/API responses,
CSV exports, or logs. The lake requires exact private ownership and modes (`0700` directories,
`0600` files), refuses symlinks and root-filesystem fallback, and must have separately governed
physical access, at-rest encryption, backup, retention, deletion, and incident response. See the
[bulk-export runbook](gsc-bigquery-bulk-export.md).

GA4 title, channel, referrer, and event families and Umami title, channel, domain, country, region,
referrer, browser, operating system, device, language, screen, hostname, tag, and event families are
individually opt-in. `umami_dimensions = ["all"]` expands only to that allowlist. It never enables
city, distinct ID, sessions, event properties, replay, or heatmap data. Enabling route paths does not
silently enable those additional dimensions.

Clicks, sessions, and visits retain their provider identity. The system does not infer people,
individual journeys, link-click causality, or location from these aggregates.
Umami's pageview-response `sessions` array is deliberately labeled `daily visitors` in the metric
catalog because that is the provider's documented meaning; it is never presented as additive
sessions or substituted for the exact-window visitor count.
