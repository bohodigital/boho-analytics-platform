# Deployment model

## Local

Install the package in a virtual environment, validate a private configuration file, and keep the web
process on loopback. Local development should use sanitized fixtures unless live access is deliberate.

## Private server

The intended first production shape is:

- A dedicated operating-system user.
- A read-only application service bound to loopback.
- A separate scheduled sync command.
- A private state directory and SQLite database.
- Credentials injected by a credential-provider adapter.
- Metadata-only service logs.
- SSH port forwarding for browser access.
- Daily database backup and periodic restore tests.

The web service does not need permission to modify provider configuration. The sync service does not
need permission to bind a public port.

## Authenticated web

Future publication uses a separate hostname and authenticated HTTPS proxy or tunnel. The origin must
validate upstream identity, then enforce application-level tenant authorization. No direct host port
is opened solely to make the application reachable.

## Service hardening

Linux service examples should use a restrictive umask, no new privileges, protected home and system
paths, a private temporary directory, allowlisted writable paths, restart limits, and measured memory
and CPU limits. Network isolation must still permit configured provider APIs and any explicitly local
analytics service.

## Rollback

Application rollback is independent from provider trackers and accounts: stop the service and timer,
restore the previous package and database backup, then validate the prior schema and health endpoint.
Deployments must not require modifying provider data to roll back the dashboard.
