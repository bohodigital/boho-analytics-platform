# V1 beta validation record

Date: 2026-07-15

## Scope

This record covers the public, connection-ready V1 beta with sanitized fixtures only. No live
provider credential, client resource, form payload, mailbox content, provider configuration, site
integration, or production data was used or changed.

## Local validation

- 36 unit/integration tests passed with `ResourceWarning` promoted to an error.
- Public-tree release verification passed after generated artifacts were removed.
- Schema-v2 full example and demo configuration validated.
- Fixture initialization and idempotent sync produced 20 cataloged points.
- Summary and forms subreport rendered matching JSON/CSV aggregates.
- A clean wheel installed in a new virtual environment and loaded its packaged SQLite migration.
- Wheel: `boho_analytics_platform-0.1.0b1-py3-none-any.whl`.
- Local wheel SHA-256: `261630d688592bc5667cdb82c06867ebe7779e3c001688a8f51dbc08558d88cd`.

## Browser validation

The installed wheel was served on loopback and exercised in the in-app browser:

- server-rendered summary loaded with fixture metrics and visible forms delivery-gap warning;
- forms and search subreport links retained the selected absolute date window;
- changing dates inside the forms subreport retained subreport scope;
- JSON and CSV API routes were already covered by the HTTP integration suite;
- a 390 x 844 viewport had no document-level horizontal overflow; the wide table scrolls only
  inside its card;
- no browser console errors were recorded;
- Host rejection, CSP, no-store responses, no permissive CORS, and read-only routes passed tests.

## Private-server validation

The committed feature branch was transferred to the canonical private-server clone and validated
with Python 3.13. The same 36 tests and public-tree verifier passed. A temporary fixture deployment
initialized SQLite, synchronized 20 points, rendered the forms CSV subreport, created an online
backup, and returned `ok` from the database integrity check. The repository remained clean after the
rehearsal. No service was installed or restarted, because live configuration and credentials remain
behind the separate connection-test gate.

## Connection-test gate

Live validation remains intentionally pending. It should connect one provider at a time with a
dedicated least-privilege credential, compare one known bounded window against the provider UI/API,
and verify that logs, SQLite, JSON, CSV, and HTML remain free of secrets and content-level form/mail
data. D1 forms state and inbox delivery evidence must be tested independently.
