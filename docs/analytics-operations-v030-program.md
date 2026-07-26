# Analytics Operations 1.0 program

Status: accepted product direction; contracts stage only  
Target release: `0.3.0`  
Release name: Analytics Operations Foundation  
Baseline: accepted `0.2.0` release, database schema 4

## Outcome

Version 0.3.0 turns the existing provider-separated warehouse and reporting surface into a
privacy-bounded analytics operating system. It adds six connected capabilities:

1. canonical goals and provider evidence reconciliation;
2. saved, provider-compatible segments;
3. annotations and a change ledger;
4. deterministic alert rules and incident lifecycle;
5. scheduled reports with audited delivery; and
6. an operations-first console.

The default page should answer what requires attention before presenting detailed charts. It must
show evidence health, important goal movement, unresolved incidents, recent changes, and concise
site summaries.

## Economic purpose

The release reduces repeated manual analytics review, makes conversion evidence usable for client
and owned-property decisions, and creates auditable recurring reporting. Its value is operational
leverage, decision quality, risk reduction, and reusable proof rather than a new data-collection
business.

## Controlling product rules

- Provider semantics remain separate. GA4 sessions, Umami visits, Search Console clicks,
  Cloudflare requests, and confirmed form outcomes are never silently summed.
- A goal names one canonical source. Other provider observations are corroborating evidence, not a
  blended conversion count.
- Browser surfaces remain read-only. Activation, annotation changes, incident acknowledgement,
  suppression, evaluation, report execution, and delivery are CLI- or scheduler-only operations.
- Definitions originate in private declarative configuration and activate as immutable,
  content-addressed versions.
- Existing `metric_facts`, acquisition coverage, sync history, watermarks, forms lineage, and graph
  evidence remain the evidence layer. No parallel analytics database is introduced.
- Raw visitor identity, raw Search Console queries, full external referrers, form content, email
  addresses from events, and unconstrained event payloads remain prohibited.
- Rules and summaries are deterministic and evidence-backed. Opaque causal or anomaly claims are
  outside the release.
- The design must remain suitable for one small private server: one global writer lease, bounded
  windows, incremental evaluation, no overlapping delivery jobs, and no new browser or Node
  runtime dependency.

## Delivery stages and gates

| Stage | Deliverable | Entry gate | Exit gate |
| --- | --- | --- | --- |
| 1 | Contracts | Accepted `0.2.0` baseline | Schema, configuration, threat, compatibility, and migration contracts reviewed |
| 2 | Schema-5 storage | Stage 1 accepted | Additive migration, backup, restore, repeated/interrupted migration tests pass |
| 3 | Goal registry | Stage 2 accepted | Canonical source and reconciliation reports pass fixtures and bounded copied-data checks |
| 4 | Segment registry | Stage 2 accepted | Deterministic compilation and provider diagnostics pass |
| 5 | Annotation ledger | Stage 2 accepted | Manual/import paths are bounded, deterministic, and idempotent |
| 6 | Alert engine | Stages 3-5 accepted | Lifecycle, maturity, coverage, deduplication, and backtests pass |
| 7 | Scheduled reports | Stages 3-5 accepted | Preview, rendering, retries, allowlists, and exactly-once delivery tests pass |
| 8 | Operations console | Stages 3-7 accepted | HTML, JSON, and CSV agree; browser remains read-only |
| 9 | Integrated compatibility | Stage 8 accepted | Security, accessibility, mobile, performance, package, and release verification pass |
| 10 | Independent acceptance | Stage 9 accepted | Copied-production migration, synthetic failures, delivery, rollback, and exact candidate pass |

Goal and segment work may proceed in parallel after schema acceptance when their writable surfaces
do not overlap. Alert and report delivery work may proceed in parallel only after the goal, segment,
and annotation contracts they consume are frozen.

## Release acceptance

The release is acceptable only when:

- schema 5 preserves all prior facts and histories;
- goals work for all configured sites without merging canonical and corroborating evidence;
- segments fail clearly when a provider cannot honor a filter;
- annotations are idempotent and appear on relevant time series;
- incidents open, continue, resolve, suppress, and acknowledge deterministically;
- incomplete periods do not trigger false incidents;
- scheduled reports render and deliver exactly once;
- failures are visible in the operations console;
- browser routes remain bounded and read-only;
- HTML, JSON, and CSV surfaces agree;
- accessibility, package, release-verifier, copied-data, and rollback checks pass; and
- no new Site Graph analysis tranche enters the release.

## Deferred boundary

Funnels, campaigns, UTM normalization, attribution, journeys, paths, and cohorts belong to a
separately accepted behavioral-analysis release. They may use only provider evidence that supports
ordered behavior and may not delay `0.3.0`.

Session replay, heatmaps, visitor profiles, browser recording, experimentation delivery, surveys,
public dashboard hosting, multi-tenancy, arbitrary browser SQL, and a second warehouse remain out of
scope.

The next full Site Graph program is gated on a settled production website revision, accepted
`0.3.0`, stable goals and segments, operating deployment annotations, at least one completed report
cycle, reviewed alert false positives, and populated route-level analytics.
