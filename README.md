# Boho Analytics Platform

Boho Analytics Platform is a lightweight, auditable foundation for consolidating website
analytics from Umami, Cloudflare, Google Analytics, and Google Search Console.

> **Project status: pre-alpha architecture foundation.** The repository currently provides
> configuration validation, domain contracts, release-safety checks, and the durable design
> for the platform. It does not yet collect provider data or serve a production dashboard.

The project is public-first: provider adapters, storage, reporting, and the web interface will
remain inspectable and reusable. Real property identifiers, client mappings, credentials,
private deployment files, and operational data belong in a separate local configuration lane.

## Design goals

- Run comfortably on a small Linux server while remaining portable to Windows and macOS.
- Keep provider credentials out of browser code, configuration files, logs, and Git history.
- Preserve each provider's metric semantics instead of inventing misleading combined totals.
- Cache normalized aggregates locally so dashboards are fast and provider quotas are respected.
- Support arbitrary report windows, reusable report definitions, and focused sub-reports.
- Start as a modular monolith with stable internal boundaries and a clear PostgreSQL migration path.
- Fail visibly when an account, scope, metric, or provider is unavailable.

## What exists today

- `boho-analytics --version`
- `boho-analytics config validate <path>`
- Versioned, strict TOML configuration parsing.
- Typed contracts for connectors, credential providers, metric stores, capabilities, sync requests,
  query windows, report definitions, and normalized metric points.
- Public-tree verification that rejects unexpected files, generated directories, internal paths,
  and common secret patterns.
- Architecture, threat model, reporting model, configuration, deployment, and roadmap documents.

## Quick start

Python 3.11 or newer is required.

```bash
python -m venv .venv
python -m pip install --editable .
boho-analytics --version
boho-analytics config validate examples/platform.example.toml
python -m unittest discover -s tests -v
python scripts/verify_release.py
```

## Planned provider ownership

| Question | Primary source |
| --- | --- |
| Visits, pages, referrers, devices, privacy-focused events | Umami |
| Edge requests, bytes, cache, status, and security signals | Cloudflare |
| Google acquisition, engagement, and configured key events | Google Analytics |
| Search queries, impressions, clicks, CTR, and position | Google Search Console |

Metrics from different sources will remain source-labeled. Visitor and user counts will not be
silently summed or deduplicated across providers.

## Architecture

Start with [the architecture](docs/architecture.md), then read:

- [Reporting model](docs/reporting-model.md)
- [Configuration](docs/configuration.md)
- [Data model](docs/data-model.md)
- [Threat model](docs/threat-model.md)
- [Deployment model](docs/deployment.md)
- [Roadmap](docs/roadmap.md)

Architectural decisions are recorded under [`docs/adr`](docs/adr).

## Security

Do not put credentials in the platform TOML file. Configuration contains only credential
references; a credential provider resolves those references at runtime. See [SECURITY.md](SECURITY.md)
for vulnerability reporting and [the threat model](docs/threat-model.md) for design controls.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). New provider work must include capability discovery,
fixture-based contract tests, explicit metric definitions, quota behavior, redaction tests, and
failure-mode documentation.

## License

MIT. See [LICENSE](LICENSE).
