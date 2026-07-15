# Boho Analytics Platform

Boho Analytics Platform is a lightweight, public-first website analytics dashboard for Umami,
Cloudflare, Google Analytics, Google Search Console, and form-delivery monitoring. It runs on
Python 3.11+ with SQLite and a dependency-free server-rendered web interface.

> **Status: connection-ready V1 beta.** Provider adapters and local workflows are implemented and
fixture-tested. Live account compatibility and least-privilege access still need to be validated
before this is called a stable release.

The browser only reads normalized local aggregates. Provider credentials stay server-side, syncs
are explicit or scheduled, and every metric remains source-labeled. The public repository contains
no client mappings, live resource IDs, credentials, submission content, or mailbox content.

## V1 capabilities

- Read-only connectors for self-hosted Umami, Cloudflare GraphQL traffic analytics, GA4 Data API,
  Search Console, Cloudflare D1 form state, and the existing comms-platform SQLite mail index.
- Configurable form monitoring that compares durable D1 submissions and notification state with
  independently observed inbox delivery counts. It never ingests form payloads or message bodies.
- Strict schema-v2 TOML configuration with environment, systemd, and no-credential references.
- SQLite WAL storage with migrations, idempotent upserts, sync ledgers, watermarks, lease locks,
  retention, integrity checks, online backup, and guarded restore.
- Saved reports, form-specific dimension filters, reusable subreports, arbitrary absolute date
  windows, previous-period comparisons, JSON, and CSV.
- A responsive, server-rendered dashboard with no JavaScript requirement, loopback binding by
  default, Host validation, restrictive CSP, no permissive CORS, and optional Basic authentication.
- Failure isolation: one unavailable provider does not erase successful results from another.

## Quick start with safe demo data

```bash
python -m venv .venv
python -m pip install --editable .
boho-analytics --config examples/platform.demo.toml config validate
boho-analytics --config examples/platform.demo.toml db init
boho-analytics --config examples/platform.demo.toml sync --start 2026-07-01 --end 2026-07-04
boho-analytics --config examples/platform.demo.toml report summary --start 2026-07-01 --end 2026-07-04
boho-analytics --config examples/platform.demo.toml serve
```

Open `http://127.0.0.1:8787`, or forward that loopback port over SSH from a private server.

The end date is exclusive. The example above reports July 1 through July 3. Use `--days 30` for
the last 30 complete local days. A browser request never triggers a provider sync.

## Configure real connections

Copy `examples/platform.example.toml` to a private, ignored location and replace placeholders with
the resource IDs actually available to the account. Put credentials in environment variables or
systemd credentials as JSON objects; never put values in TOML.

```json
{"api_token":"replace-at-runtime"}
```

Then initialize, probe, and sync one connection at a time:

```bash
boho-analytics --config /private/platform.toml db init
boho-analytics --config /private/platform.toml probe --connection example-umami
boho-analytics --config /private/platform.toml sync --connection example-umami --days 30
```

See [configuration](docs/configuration.md), [forms monitoring](docs/forms-monitoring.md), and
[provider behavior](docs/providers.md), and [deployment](docs/deployment.md) before connecting live data.

## Metric ownership

| Question | Primary source |
| --- | --- |
| Visits, pages, sessions, privacy-focused usage | Umami |
| Edge requests, visits, and response bytes | Cloudflare |
| Google acquisition, engagement, and key events | Google Analytics |
| Search impressions, clicks, CTR, and position | Google Search Console |
| Accepted submissions and notification state | Cloudflare D1 forms database |
| Notification messages observed in a mailbox | Forms inbox adapter |

Cloudflare traffic is not treated as a substitute for browser analytics. Visitor/user counts from
different providers are never silently summed or deduplicated.

## Security boundary

Keep the web service on loopback and reach it with an SSH port forward. For future remote access,
put it behind an authenticated HTTPS proxy, keep the origin private, and add tenant authorization
before exposing client data. Basic authentication is only a small deployment control; it is not a
replacement for HTTPS or an identity-aware proxy.

Read [SECURITY.md](SECURITY.md) and the [threat model](docs/threat-model.md). Architecture and data
contracts are documented under [docs](docs/architecture.md).

## Development

```bash
python -m unittest discover -s tests -v
python scripts/verify_release.py
python -m pip wheel . --no-deps --wheel-dir dist
```

See [CONTRIBUTING.md](CONTRIBUTING.md). The project is MIT licensed.
