# Site graph security boundary

Site repositories and manifests are untrusted inputs. v1 defaults to read-only, source-only analysis and does not execute repository-provided commands.

## Threat model

The principal threats are YAML object construction, aliases that expand unexpectedly, duplicate keys that disguise policy, path traversal, credential disclosure, arbitrary build execution, resource exhaustion, regex failures, SQL injection, and findings that cannot be traced to evidence.

The contract layer responds as follows:

- PyYAML's safe loader is further restricted to reject aliases, anchors, explicit tags, duplicate keys, unknown fields, and secret-shaped field names.
- Manifest size, list sizes, selector lengths, and maximum page counts are bounded.
- Repository paths must be absolute and traversal-free. Build output paths must be relative and traversal-free.
- Repository remotes may not contain credentials, query strings, or fragments.
- `build.adapter_command` must be null. Build execution must use reviewed, built-in adapters; it must never interpolate a manifest string into a shell.
- Route regexes are compiled at validation time and reject backreferences, lookarounds, nested quantified groups, and repeated wildcards. Ingest code must still apply per-file and total-run bounds before using them at scale.
- Storage uses parameterized SQL, foreign keys, transaction rollback, immutable hashes, and exact manifest/revision provenance.
- The validation CLI emits a field allowlist and excludes paths, remotes, and account references.
- The web read model excludes source paths, repository identities, remotes, evidence excerpts, and
  manifest account references. Its queries are bounded before SVG and table rendering.

No credential is required for the manifest validator or database schema. Cloudflare entries are identifiers for a separately configured read-only integration, not secrets. Do not place tokens, passwords, private keys, cookies, or authenticated repository URLs in a site-graph manifest.

The browser surface is strictly read-only. `GET /site-graph` and `GET /api/v1/site-graph` may select a
stored site, page neighborhood, and link layers; they cannot start ingest, build, compilation, sync,
or provider operations. The routes retain the existing loopback-first and Host-validation boundary.

## Repository and build-mode gate

Repository inspection uses a fixed Git argument allowlist, disables prompts and optional locking,
bounds output, validates every tracked path, and reads the selected revision through Git object
commands. Clean worktrees are required by default. A dirty snapshot requires both a manifest that
permits it and the explicit `--allow-dirty-snapshot` operator flag; that mode is for non-production
evidence only.

Schema v1 can express `analysis.mode: build`, but only with `build.enabled: true`, a safe relative
output directory, and a supported adapter. The manifest cannot select a shell command. The current
runtime fails build mode closed unless an operating-system network-isolation runner is available;
source-only analysis remains the supported default. This prevents a repository build from silently
receiving provider credentials or unrestricted network access.

## Recovery

Before migrating operational state, create a database backup with the existing CLI. Verify it with `PRAGMA integrity_check`. Restore remains explicitly confirmed and creates a pre-restore backup when replacing an existing database.
