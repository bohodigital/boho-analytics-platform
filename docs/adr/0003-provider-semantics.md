# ADR 0003: Preserve provider metric semantics

- Status: Accepted
- Date: 2026-07-15

## Context

Analytics providers count users, visits, bots, modeled identities, search results, and edge requests
differently. Combining similarly named values can create convincing but false business conclusions.

## Decision

Every metric retains a provider source and versioned definition. Cross-provider display is allowed,
but merging requires an explicit derived-metric definition with formula, units, limitations, and
tests. Default dashboards do not sum visitors or users across sources.

## Consequences

Reports are more honest and auditable. Users may see several values for a superficially similar
question, so the interface must explain definitions and recommended use.
