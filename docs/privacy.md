# Privacy boundary

Provider connectors collect only bounded aggregate facts. Route observations are disabled by default
and must be explicitly enabled per binding.

The platform never persists raw sessions, visitor/client/distinct IDs, IP addresses, user agents,
city-level locations, form payloads, emails, phone numbers, raw Search Console queries, arbitrary
event parameters, unrestricted query strings, or full external referrer URLs. Internal URLs pass a
shared canonical route normalizer; external referrers are discarded unless their hostname is on the
binding allowlist, in which case only that domain is stored.

Configured Search Console query clusters are acquisition filters only. Stored facts contain the
configured cluster ID and aggregates, never the provider query dimension or returned query text.
Provider diagnostics expose bounded categories and limits, not request or response bodies.

GA4 title, channel, referrer, and event families and Umami title, channel, domain, device, country,
and event families are individually opt-in. Enabling route paths does not silently enable those
additional dimensions.

Clicks, sessions, and visits retain their provider identity. The system does not infer people,
individual journeys, link-click causality, or location from these aggregates.
