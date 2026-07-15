# Configuration

The platform uses versioned TOML for non-secret configuration. TOML is available through Python's
standard library and is explicit enough for review and source control.

See [`examples/platform.example.toml`](../examples/platform.example.toml).

## Entities

- `platform`: default timezone and state location.
- `clients`: ownership and future authorization boundary.
- `sites`: canonical web properties.
- `connections`: provider account plus a credential reference.
- `bindings`: mapping from a site to a resource available through a connection.

One connection may serve several sites, and one site may bind to several providers. This supports
client-owned accounts, delegated accounts, partial permissions, and provider gaps without changing
the application schema.

## Secrets

`credential_ref` is an opaque locator, not a credential. Examples include:

- `env:NAME` for deliberate local development.
- `systemd:NAME` for systemd credentials.
- `keyring:NAME` for an operating-system keyring adapter.
- `plugin-name:REFERENCE` for an installed credential-provider plugin.

Inline keys such as `password`, `token`, `api_key`, `client_secret`, and `refresh_token` are rejected
anywhere in the document.

## Evolution

`schema_version` changes only when a configuration migration is required. Unknown fields are errors,
not ignored extensions. Future optional settings belong under explicitly documented tables or
connection `options` whose values remain non-secret.
