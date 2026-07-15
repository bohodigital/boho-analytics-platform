# ADR 0002: Separate public core from private deployment state

- Status: Accepted
- Date: 2026-07-15

## Context

The tool should be publicly inspectable and reusable, while real installations contain credentials,
client identities, property mappings, report definitions, and operational paths that are not public.

## Decision

Keep generic source, tests, schemas, examples, and public documentation in this repository. Keep every
real deployment overlay outside it. Private installations use configuration and plugin interfaces;
the public package never imports deployment-specific code.

## Consequences

Public release becomes the normal development lane instead of a later sanitization exercise. Local
operators must manage a second, private configuration lifecycle and test compatibility with public
package versions.
