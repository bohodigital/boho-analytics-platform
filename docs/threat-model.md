# Threat model

## Assets

- Provider credentials and refresh tokens.
- Client analytics and search-query data.
- Saved report definitions and generated exports.
- Tenant/site mappings.
- Application sessions and authorization decisions.

## Trust boundaries

- Browser to web application.
- Web application to local metric store.
- Sync process to credential provider.
- Sync process to external provider APIs.
- Public package to private deployment configuration.
- Maintainer workstation and CI to the public repository.

## Primary threats and controls

### Credential exposure

- Configuration stores only opaque credential references.
- Provider calls happen server-side.
- Connectors receive short-lived credential leases where supported.
- Errors and logs use allowlisted metadata.
- Public-tree and CI scans reject common credential patterns and private paths.

### Cross-tenant disclosure

- Every stored and queried record carries tenant/site scope.
- Repository methods require explicit scope rather than applying a global default.
- Authorization is tested at the report-engine boundary, not only in UI routes.
- Exports inherit the same authorization and receive non-cacheable responses.

### Browser attacks

- Loopback binding by default.
- Strict host validation and no permissive CORS.
- CSRF protection and origin validation for state-changing routes.
- HttpOnly, SameSite sessions and Secure cookies under HTTPS.
- A restrictive Content Security Policy with local assets.
- No state-changing GET routes and no provider credential endpoints.

### SSRF and connector abuse

- Provider endpoints come from trusted administrator configuration.
- Connector-specific URL policy validates scheme, host, redirects, and allowed local endpoints.
- Untrusted report filters never become URLs or SQL fragments.
- Outbound requests have timeouts, response-size limits, and bounded redirects.

### Supply-chain compromise

- Keep runtime dependencies small and purposeful.
- Pin CI actions to full commit SHAs.
- Use least-privilege workflow permissions.
- Review dependency additions as architectural changes.
- Build release artifacts from a clean, verified source tree.

### Data corruption or misleading reports

- Idempotent writes and database migrations.
- Backups before destructive migrations.
- Provider, metric definition, window, timezone, completeness, and watermark on outputs.
- Cross-provider values are never silently combined.

## Deployment obligations

An Internet-accessible deployment additionally requires authenticated HTTPS, origin-side identity
validation, tenant-scoped authorization, audit logging, rate limiting, tested backup recovery, and a
documented incident-response path.
