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
| `cloudflare-forms` | `account_id`, `database_id` | forms `site_id` |
| `forms-inbox` | `database_path` | mailbox key |
| `fixture` | `path` | fixture resource ID |

Forms inbox binding options are `mailbox_key`, `sender_contains`, and `subject_contains`. Use the
narrowest stable filters available so unrelated inbound mail is not counted. These strings remain
private deployment configuration even though they are not credentials.

## Reports and subreports

Each report has a client, one or more sites, a metric allowlist, and a default window. A subreport
uses a narrower metric set and may define exact dimension filters:

```toml
[[reports.subreports]]
id = "contact-form"
title = "Contact form delivery"
metric_ids = ["forms.submissions", "forms.sent", "forms.failed"]
default_window_days = 30

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
