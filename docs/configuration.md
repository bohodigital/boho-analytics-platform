# Configuration

V1 uses strict schema-v2 TOML. Unknown fields, duplicate identifiers, invalid references, invalid
timezones, non-loopback unauthenticated binding, and secret-like keys are errors. Start with
[`examples/platform.example.toml`](../examples/platform.example.toml); keep the real copy outside
the public repository.

## Top-level tables

- `platform`: timezone, SQLite path, default sync window, HTTP timeout, and response-size limit.
- `web`: bind host, port, Host allowlist, and authentication policy.
- `retention`: hourly and daily fact retention.
- `clients`: future authorization boundary.
- `sites`: canonical properties and reporting timezones.
- `connections`: provider adapter, credential reference, and non-secret adapter options.
- `bindings`: site-to-resource mappings, metric groups, and binding-specific options.
- `reports`: saved report scope, metrics, default window, subreports, and dimension filters.

One connection may serve several sites, and one site may use several providers. Missing client
ownership or provider scopes can therefore be represented without changing the schema.

## Credential references

`credential_ref` is a locator, never a credential:

- `env:NAME`: read one environment variable. The value is normally a JSON object.
- `systemd:NAME`: read a file inside `CREDENTIALS_DIRECTORY`.
- `none:LABEL`: explicit no-credential source for fixtures and local read-only SQLite.

Supported credential fields are:

| Connector | Credential JSON fields |
| --- | --- |
| Umami | `api_key`, `token`, or `username` and `password` |
| Cloudflare traffic/forms | `api_token` |
| Google | `access_token`; OAuth `refresh_token`, `client_id`, `client_secret`; or service-account fields |
| Forms inbox/fixture | none |

Google service accounts require `pip install 'boho-analytics-platform[google]'`. Short-lived access
tokens and refresh-token exchange work without the optional SDK. Inline keys including `password`,
`token`, `api_key`, `client_secret`, and `refresh_token` are rejected anywhere in TOML.

## Provider options and binding resources

| Provider | Connection options | Binding resource |
| --- | --- | --- |
| `umami` | `base_url` | website ID |
| `cloudflare` | none | zone tag |
| `google-analytics` | none | GA4 property ID |
| `search-console` | none | URL-prefix property or `sc-domain:` property |
| `cloudflare-forms` | `account_id`, `database_id`, `source_retention_days` | forms `site_id` |
| `forms-inbox` | `database_path` | mailbox key |
| `fixture` | `path` | fixture resource ID |

Any binding may opt into a strict site-local observation floor:

```toml
[bindings.options]
observation_start = "2026-07-12"
```

The value must use `YYYY-MM-DD` and means the first local date on which that
site/provider binding is known to have trustworthy instrumentation or source
coverage. Reporting keeps the originally requested cells: facts and successful
sync runs cannot prove daily cells before the boundary, so a window that crosses
the boundary remains partial instead of being silently shortened. Exact-window
facts that span the boundary may remain visible as partial evidence, but cannot
make a decision input complete; a window entirely before the boundary remains
unknown. Existing rows are retained as lineage.

`cloudflare-forms.source_retention_days` is required and must match the forms Worker's verified D1
retention policy; the Boho deployment uses `90`. Values above 90 are rejected. Sync windows must use
complete site-local days, end no later than the current site-local day, and start after the local
retention cutoff day. The connector fails closed instead of manufacturing zeroes outside that
trustworthy horizon.

Forms inbox binding options are `mailbox_key`, `sender_contains`, `subject_contains`, the optional
`subject_excludes` array, and the generic `observation_start`. Matching is case-insensitive substring matching.
Use the narrowest stable filters available so unrelated inbound mail is not counted, and configure
stable synthetic-notification markers explicitly, for example:

```toml
[bindings.options]
mailbox_key = "forms"
sender_contains = "forms-sender.example"
subject_contains = "[Boho form] Project inquiry"
subject_excludes = ["Project inquiry - Boho Forms Live Canary"]
observation_start = "2026-07-13"
```

Exclusions apply before both delivery and unread counts. Up to 16 unique, non-empty markers of 128
characters each are accepted. For this connector, `observation_start` additionally authorizes
quiet-day zero facts on and after the independently verified mail-index boundary. Without it, the
connector emits facts only for matching messages and does not invent quiet-day zeroes. These values remain private deployment configuration
even though they are not credentials.

## Acquisition-detail controls

Detailed acquisition families are disabled per binding unless
`bindings.options.route_analytics.enabled = true`. `max_days`, `page_size`, and `max_pages` bound
each read. They are safety ceilings, not evidence that a provider returned every possible row.

For Search Console, `search_type` selects one explicit surface (`web`, `image`, `video`, `news`,
`discover`, or `googleNews`). Use `search_types = ["all"]` to collect all six as separate provider
scopes; it cannot be combined with the singular setting. `search_console_dimensions = ["all"]`
enables the device, country, and search-appearance views. `search_console_query_text` opts into
privacy-screened query wording;
rejected wording is counted in a single redacted bucket rather than persisted. The more expensive
page/query view requires `search_console_page_query = true` as well. `search_console_hourly = true`
adds Google's recent `hourly_all` provisional rows within its supported lookback. Query, page,
country, appearance, and other high-dimensional Search Analytics reads are provider top-row views,
not exhaustive exports. When neither pagination bound is configured, Search Console detail uses
25,000 rows per page and three calls so two full pages plus the required terminal empty call can
prove the API's 50,000-row ceiling. Smaller explicit bounds remain valid but fail closed at the cap.
Average position is defined only for Google Search result surfaces (`web`, `image`, `video`, and
`news`). Discover and Google News collect clicks, impressions, and CTR without inventing a zero
position. Those two surfaces also do not expose search-query wording, so query and page/query reads
are skipped even when the binding enables those optional families. Discover's report also does
not expose a device grouping, so a configured `device` route dimension is skipped for Discover
while remaining enabled for the Google Search and Google News surfaces that support it.

For Umami, `umami_dimensions = ["all"]` expands to every supported privacy-safe aggregate:
browser, channel, country, device, domain, event name, hostname, language, operating system,
referrer, region, screen, tag, and title. A subset may be listed instead. City, distinct ID, raw
sessions, event properties, replay, and heatmap data are deliberately outside the ingestion
contract. `umami_event_names` selects named daily event series without event properties.
Umami's response field named `sessions` is cataloged as `umami.daily-visitors`, matching the
provider's documented visitor semantics; exact-window unique visitors remain `umami.visitors`.

Google Trends access is separate from both GA4 and Search Console. Do not broaden an existing
Search Console credential in anticipation of Trends: the official Trends API is allowlisted and
its approved-project setup is supplied by Google after admission.

## Reports and subreports

Each report has a client, one or more sites, a metric allowlist, and a default window. Set
`default_end_lag_days` when the default view should stop before provider-finalization lag. The lag
applies only when dates are omitted; explicit `start` and `end` values are never shifted. Subreports
inherit the report lag unless they set their own value. A subreport uses a narrower metric set and
may define exact dimension filters:

```toml
[[reports.subreports]]
id = "contact-form"
title = "Contact form delivery"
metric_ids = ["forms.submissions", "forms.sent", "forms.failed"]
default_window_days = 30
default_end_lag_days = 1

[reports.subreports.filters]
form_id = "contact"
```

Filters are data, not SQL. Only canonical stored dimensions are compared. The same definition drives
HTML, JSON, and CSV.

## Web policy

The safe default is `127.0.0.1`, allowed hosts `127.0.0.1` and `localhost`, and `auth_mode = "none"`.
Unauthenticated non-loopback binding is rejected. `auth_mode = "basic"` requires `username` and an
`auth_credential_ref` whose credential contains `password` or `value`.

Do not expose the built-in HTTP server directly to the Internet. Use loopback plus SSH forwarding,
or an authenticated HTTPS reverse proxy with the origin kept private.

## Changes and validation

`schema_version` changes only when migration is required. Validate before restarting services:

```bash
boho-analytics --config /private/platform.toml config validate
```
