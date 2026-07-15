# Contributing

Thank you for helping build an analytics platform that is useful, inspectable, and honest about
what its data means.

## Development setup

```bash
python -m venv .venv
python -m pip install --editable .
python -m unittest discover -s tests -v
python scripts/verify_release.py
```

## Change expectations

- Keep the public/private boundary intact.
- Add tests for successful behavior and important failures.
- Never add provider credentials, real client identifiers, internal paths, or production exports.
- Keep connectors read-only by default.
- Define metric units, aggregation rules, completeness, and source semantics before exposing them.
- Treat unavailable permissions and quotas as normal capability states, not exceptional crashes.
- Update an ADR when changing a foundational boundary or dependency direction.

## Provider contributions

A provider adapter is not complete until it includes:

1. A capability probe.
2. Explicit supported resource and metric groups.
3. Authentication and least-privilege documentation.
4. Pagination, rate-limit, retry, and backfill behavior.
5. Sanitized fixtures and contract tests.
6. Redacted errors and metadata-only logs.
7. Freshness and completeness semantics.

## Pull requests

Keep changes focused. Explain the user impact, data semantics, security impact, validation, and
rollback. Public API or schema changes must include a migration or compatibility note.
