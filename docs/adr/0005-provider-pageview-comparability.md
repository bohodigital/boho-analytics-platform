# ADR 0005: Compare provider pageviews only on mature complete overlap

- Status: Accepted
- Date: 2026-07-30

## Context

GA4 and Umami expose superficially similar pageview measures with different collection, filtering,
identity, and processing semantics. Historical reports had provider windows with different first
available dates and data-through limits. Comparing full-range totals in those conditions produced
numeric differences that looked meaningful even though the covered dates were unequal. Umami route
visits also cannot serve as a pageview control total.

Route-level acquisition can be incomplete because provider pagination has a configured bound or
because an unsafe dimension is rejected. A partial safe slice is useful evidence, but it cannot
prove the complete route population or reconcile a headline count.

## Decision

Acquire provider-correct route pageviews as separate metrics:

- GA4 uses `pagePath` and `screenPageViews` for `google.page-path-views`; and
- Umami uses `metrics/expanded` with `type=path` and `field=pageviews` for
  `umami.route-pageviews`.

Both connectors paginate with explicit bounds. GA4 exhaustion requires exact requested
`date`/dimension and metric header names, exact two-dimension/one-metric row arity, bounded metric
values, a consistent bounded `rowCount` on every page, and nonoverlapping raw dimension identities;
a short or repeated page cannot substitute for missing completeness metadata. Umami exhaustion also
requires unique raw `name` identities across and within pages so an overlapping page is never
counted twice. Umami availability must contain the exact requested half-open
timestamp interval. Both headline adapters require an explicit pageview series and exact
configured-site-local whole-day request boundaries. Provider dates must remain inside the requested
window, and pageview values must be non-negative integral counts. Any rejected row or unproven
boundary downgrades retained safe facts to `UNKNOWN`. Queries, fragments, external URLs, exclusions,
encoded separators, backslashes, control characters, opaque identifiers on identity-bearing routes, and
other unsafe path dimensions are rejected by the privacy normalizer without retaining the rejected
value.

Rows that collapse to the same normalized route are aggregated with exact integers and revalidated
before persistence. Out-of-domain collision sums fail closed. Reporting reads route facts only for
the exact current reconciliation window; previous-period and retained-history queries remain
headline-only. Evidence-state classification uses exact integer cross-products at the daily 0.8 and
1.25 boundaries before bounded exact aggregate totals, so Decimal rounding and opposing daily
divergences cannot change the interpretation.

Reporting compares provider headlines by site only on the intersection of mature complete calendar
dates in the requested window. Exactly half of multiple paired mature dates diverging is `unknown`;
a minority is isolated, a majority persistent, and the only paired date remains isolated. Native
facts are attributable to a current provider binding only
when exactly one successful data run contains the fact observation timestamp and exact fact
interval, that unambiguous run belongs to the current binding, and it is the latest successful
snapshot covering that cell. Older route rows missing from a later snapshot are not mixed into
reconciliation. Complete quiet dates likewise require one run to contain the exact already-closed
local day; adjacent partial runs cannot be merged. GA4 and Umami runs additionally require the
post-upgrade explicit-pageviews result marker stored in the existing ledger field. Legacy
`data`/`empty` rows cannot prove coverage or attribution and remain untouched, so an upgrade requires
a fresh sync without a schema bump, migration, or historical rewrite. Replacing a binding therefore
makes facts from the old resource ineligible without rewriting historical fact bytes or changing the
schema. It exposes provider-only dates and coverage metadata but excludes those dates from
comparison totals. A missing intersection is `non_comparable` with null numeric comparison fields.
The implementation never blends, averages, substitutes, or ranks the providers.

A route sum reconciles only against its own provider headline over an identical complete date set.
Every fact must be contained by the comparison window and span exactly one configured site-local
calendar day, from midnight to the following midnight, independent of the report timezone. Every
retained provider pageview value and exact daily sum is revalidated as finite, non-negative,
integral, and bounded before it can contribute; an invalid cell is incomplete rather than zero.
Every date must have final route facts and an exact daily sum. Otherwise reconciliation is withheld
with a bounded reason. Series and plot paths enforce acquisition-marker authorization for requested
native headline facts but do not materialize route facts or execute comparison construction;
dedicated comparison and report paths retain the bounded behavior. Sync and reporting project one
requested calendar-date interval into each binding's configured site timezone, preserving exact
local-day acquisition, fact queries, coverage, series labels, and provider comparison for
mixed-timezone configurations.

## Consequences

Past unequal full-range examples become non-comparable or explicitly coverage-qualified. Users can
still inspect both providers and every retained historical fact without receiving false precision.
HTML, JSON, and CSV share one comparison model, including first availability, data-through, exact
possibly discontinuous paired and provider-only date ranges, paired totals, difference, ratio,
low-volume warning, evidence state, semantics, coverage limits, and route reconciliation.

The comparison remains diagnostic. Divergence can identify a tracking discrepancy worth
investigating, but it does not establish which provider is correct or prove whether traffic was
human, automated, or synthetic.
