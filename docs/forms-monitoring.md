# Forms monitoring

V1 treats form acceptance and notification delivery as separate facts:

1. The forms backend durably stores a submission in Cloudflare D1.
2. The forms backend attempts its notification workflow and records `pending`, `sent`, or `failed`.
3. The mail synchronization service independently observes a matching inbound notification.
4. The dashboard compares aggregate counts and surfaces a delivery gap.

This distinction prevents an email outage from being mistaken for a lost form submission.

## Privacy boundary

The D1 connector sends one fixed aggregate SQL query. It selects only date, form ID, notification
status, and count. It never selects the submission payload. The inbox connector opens SQLite with
`mode=ro` and `PRAGMA query_only=ON`; it selects only date and counts. Sender and subject columns may
be used in configured `WHERE` filters but are not selected, stored, logged, returned, or exported.

The platform must never receive or retain:

- form field values or submission payload JSON;
- message bodies, previews, or search indexes;
- sender or recipient address values;
- IP addresses, user agents, Turnstile tokens, or provider credentials.

Tests assert that forbidden form fields are absent from D1 SQL and fixture data.

## Configuration

Use a Cloudflare API token restricted to the account and D1 database needed for reporting. The
Cloudflare API currently exposes D1 query under a permission named D1 Read/Write even though this
connector only issues `SELECT`. Treat that provider-side permission mismatch as a risk: use a
dedicated token and account restrictions, and revoke it if the connector ever attempts a write.

For inbox evidence, point `database_path` at the existing mail index and grant the analytics service
read-only filesystem access. Configure a mailbox key plus stable sender/subject fragments. The
analytics platform does not own mailbox sync and never writes to that database.

For a form-specific report, store `form_id` as a D1 aggregation dimension and add a subreport filter:

```toml
[reports.subreports.filters]
form_id = "contact"
```

Inbox evidence is mailbox/filter level in V1. If one mailbox receives several forms with identical
notifications, the inbox count cannot reliably attribute delivery to a form ID; use distinct stable
subject markers or interpret the inbox result as aggregate delivery evidence.

## Operational interpretation

- `submissions > inbox_deliveries`: notification delay, retry, filtering mismatch, sync lag, or mail
  delivery failure. The durable D1 record remains authoritative.
- `failed > 0`: the forms notification workflow recorded a terminal failure and needs attention.
- `pending > 0`: may be normal briefly; persistent counts require investigation.
- `inbox_deliveries > submissions`: filters are too broad, the selected windows differ, duplicate
  notifications exist, or D1 retention removed older source rows.

The dashboard reports the discrepancy; it does not resend mail, mutate D1, mark messages read, or
change form routing.
