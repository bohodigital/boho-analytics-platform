# Analytics Operations compatibility

Compatibility is evaluated against stored catalog facts, not against features a provider might
offer in another API or plan. A provider capability does not become supported until the connector,
catalog dimension set, coverage semantics, fixtures, and query compiler all support it.

## Initial segment matrix

`Supported` means the current evidence model can honor the logical filter. `Partial` means only a
documented metric or observation scope can honor it. `Unsupported` means compilation must fail.

| Logical dimension | GA4 | Umami | Search Console | Cloudflare | Forms |
| --- | --- | --- | --- | --- | --- |
| site | Supported | Supported | Supported | Supported | Supported |
| provider | Supported | Supported | Supported | Supported | Supported |
| route | Partial: page and landing scopes | Partial: path, entry, exit | Partial: page scopes | Unsupported | Unsupported |
| landing route | Supported on landing metrics | Supported on entry metrics | Unsupported | Unsupported | Unsupported |
| source | Unsupported in accepted facts | Unsupported | Unsupported | Unsupported | Supported only as provider identity |
| medium | Unsupported | Unsupported | Unsupported | Unsupported | Unsupported |
| channel | Supported on channel metrics | Supported on channel metrics | Unsupported | Unsupported | Unsupported |
| campaign | Unsupported | Unsupported | Unsupported | Unsupported | Unsupported |
| device | Unsupported in accepted GA4 facts | Supported on device metrics | Supported on route/device scope | Unsupported | Unsupported |
| country | Supported on country metrics | Supported on country metrics | Supported on country or route/country scope | Supported on country metrics | Unsupported |
| region | Supported on region metrics | Supported on region metrics | Unsupported | Unsupported | Unsupported |
| goal | Derived after a compatible goal exists | Derived after a compatible goal exists | Derived only for compatible goal evidence | Derived only for compatible goal evidence | Derived for compatible form goals |
| event | Supported for configured event names | Supported for configured event names | Unsupported | Unsupported | Unsupported |
| date | Supported | Supported | Supported with provider date basis | Supported | Supported with site-local basis |
| completeness | Supported | Supported | Supported; page scopes remain `UNKNOWN` | Supported | Supported |

This matrix intentionally marks `source`, `medium`, `campaign`, and GA4 `device` unsupported where
the accepted schema-4 facts do not preserve those dimensions. A later connector tranche may add
bounded facts and then revise the matrix through tests. The compiler must not pretend provider API
capability is stored evidence.

## Goal binding checks

Binding validation requires all of:

1. catalog metric exists;
2. declared source and unit equal the catalog definition;
3. requested route or event rule matches an allowed dimension set;
4. aggregation behavior is compatible with the metric;
5. observation scope is explicit for route facts;
6. canonical coverage is sufficient for the requested acceptance fixture; and
7. any denominator has compatible site, window, grain, and completeness semantics.

The initial trustworthy canonical candidates are forms facts for confirmed form outcomes and
explicitly configured site events for supported event goals. Page engagement, traffic, and search
facts are proxies unless a goal definition explicitly and honestly makes them canonical.

## Report compatibility

| Consumer | Goals | Segments | Annotations | Alerts | Subscription |
| --- | --- | --- | --- | --- | --- |
| Existing reports | additive sections only | compiled per selected metric | markers on compatible time series | links only | may render |
| Route observations | route-compatible goals | route-compatible predicates | route/site markers | may consume compatible rules | may render |
| Comparisons | same definition version or disclosed version change | identical compiled segment version | period context | comparison rule evidence | may render |
| CSV and JSON | required parity | required diagnostics | required export | required export | delivery history only |
| Browser | read-only | read-only | read-only | read-only | status only |

## Provider semantic comparisons

Allowed configured divergence pairs include GA4 sessions versus Umami visits, GA4 views versus
Umami page visits, GA4 key events versus confirmed forms, and Cloudflare traffic versus browser
analytics. Each side retains its own metric, unit, coverage, date basis, and maturity lag. The
comparison produces two observations and a divergence calculation, never a blended value.

## Definition-version compatibility

Historical outputs retain the definition version used when calculated. A comparison across a
definition activation boundary is unavailable by default. It may be shown only when:

- both versions declare the same source, unit, denominator, aggregation, and filter semantics; or
- the report explicitly presents two labeled series and discloses the definition change.

Silent rebasing of old outcomes, segments, alert evaluations, or deliveries is prohibited.
