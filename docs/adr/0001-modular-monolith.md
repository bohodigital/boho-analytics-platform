# ADR 0001: Start as a modular monolith

- Status: Accepted
- Date: 2026-07-15

## Context

The platform needs several providers, scheduled ingestion, storage, reporting, and a web interface,
but its initial workload fits on a small server and is operated by a small team.

## Decision

Use one Python package with explicit module contracts, one sync process, one web process, and one
database. Do not introduce a message broker, distributed scheduler, separate frontend service, or
microservices until measured load or ownership boundaries require them.

## Consequences

Deployment and debugging remain simple. Connector and storage contracts preserve replacement paths.
A poorly maintained module boundary could still create coupling, so architecture tests and reviews
must enforce dependency direction.
