# Security policy

## Supported versions

The project is a V1 beta. Until the first stable release, security fixes are applied only to the
latest commit on the default branch.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability or credential exposure. Use the
repository's private vulnerability reporting feature or a private GitHub Security Advisory.

Include the affected commit or version, impact, reproduction steps, and any suggested mitigation.
Do not include real credentials, client analytics, personal data, or production database contents.

## Security boundaries

- This public repository must never contain live provider credentials or private client mappings.
- Browser code must never receive provider credentials.
- Connectors are read-only unless a separately reviewed feature explicitly documents otherwise.
- Raw provider payload retention is disabled in the SQLite acquisition and browser-reporting plane.
- Search Console BigQuery aggregate rows are an explicit private-lake exception: they require a
  dedicated least-privilege reader, a UUID-verified external filesystem, private ownership/modes,
  cost limits, integrity manifests, and no path into SQLite, HTTP, CSV, or logs.
- Logs must contain metadata and status categories, not credentials or private report rows.
- Public deployment requires authenticated HTTPS and origin-side identity validation.
- The forms connectors must never select or persist submission payloads, message bodies, or address
  values; report aggregate-only violations privately as security issues.

See [`docs/threat-model.md`](docs/threat-model.md) for the maintained threat model.
