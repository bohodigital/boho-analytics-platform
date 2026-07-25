# Artifact Evidence Security

Artifact Evidence Core 2.1 treats every supplied build output and deployment
metadata file as untrusted bytes, even when the owner authorized the location.
The adapter reads local evidence only. It never imports application code, runs a
package script or build, opens a browser, calls a deployment provider, or writes
provider state.

## Authorization boundary

Callers provide exact artifact roots or archives and an exact 40- or 64-character
revision. Directory roots, files inside them, archives, and deployment metadata
must be regular files. Symlinks, hard links, archive links, special files,
absolute paths, Windows-drive paths, backslashes, parent traversal, duplicate
archive paths, and encrypted ZIP entries fail closed.

## Resource bounds

The parser enforces:

- 4 MiB per supported artifact file;
- 64 MiB total expanded supported content;
- 64 MiB per compressed archive;
- 20,000 input entries and supported files;
- a 1,000:1 ZIP expansion-ratio ceiling;
- 200,000 route-manifest JSON nodes;
- 100,000 sitemap locations;
- 256 KiB and 10,000 nodes for deployment metadata.

All text must be strict UTF-8 without NUL bytes. Sitemap declarations and
entities are rejected. JSON duplicate keys, non-finite numbers, excess nesting,
oversized strings, and excess list entries fail closed.

## Stored surface

Only deterministic, relative evidence paths are emitted. Route extraction from
JSON is restricted to known route-bearing fields; arbitrary slash-prefixed
strings do not become routes. Link query strings are removed, credentials and
unsafe schemes are rejected, and `mailto:` or `tel:` targets retain only their
action class. Deployment metadata retains only allowlisted public fields and
rejects secret-shaped keys anywhere in the document.

Content hashes cover exact input bytes. The aggregate evidence hash covers the
parser version, supplied revision, normalized evidence, coverage, and
diagnostics. Deployment and route-manifest revisions are reported as `matched`,
`mismatched`, `conflicting`, or `unchecked`; mismatches are evidence, not silently
treated as agreement.

These controls limit parser exposure but do not prove that an artifact was
produced by a particular build. Cryptographic provenance or an authenticated
deployment API belongs behind a separate approved trust boundary.
