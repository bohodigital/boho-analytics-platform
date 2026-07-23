"""Privacy-bounded, provider-labeled geographic reporting."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal


SOURCE_CONFIG = {
    "umami": {
        "country_metric": "umami.country-visits", "region_metric": "umami.region-visits",
        "label": "Umami visits", "grain": "exact-window",
        "methodology": "Umami country and region codes derived from privacy-bounded IP geolocation; IP addresses are not stored here.",
    },
    "google-analytics": {
        "country_metric": "google.country-sessions", "region_metric": "google.region-sessions",
        "label": "GA4 sessions", "grain": "daily additive",
        "methodology": "Google Analytics sessions grouped by country and provider-reported region.",
    },
    "search-console": {
        "country_metric": "search.country-clicks", "region_metric": None,
        "label": "Search Console clicks", "grain": "daily additive",
        "methodology": "Google Search Console search clicks grouped by country; state and county dimensions are not provided.",
    },
    "cloudflare": {
        "country_metric": "cloudflare.country-visits", "region_metric": None,
        "label": "Cloudflare visits", "grain": "daily additive, adaptive/provisional",
        "methodology": "Cloudflare adaptive edge visits grouped by country; values remain provisional and are not rescaled.",
    },
}

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
STATE_CODES_BY_NAME = {name.casefold(): code for code, name in US_STATES.items()}


def _number(value: Decimal):
    return int(value) if value == value.to_integral_value() else float(value)


class GeographyService:
    def __init__(self, config, store, *, suppression_threshold: int = 3) -> None:
        if suppression_threshold < 1:
            raise ValueError("geography suppression threshold must be positive")
        self.config = config; self.store = store; self.suppression_threshold = suppression_threshold

    def render(self, report_id, window, source, *, site_id=None):
        if source not in SOURCE_CONFIG:
            raise ValueError("unknown geography source")
        report = next((item for item in self.config.reports if item.id == report_id), None)
        if report is None:
            raise ValueError("unknown report")
        if site_id is not None and site_id not in report.site_ids:
            raise ValueError("unknown report site")
        site_ids = (site_id,) if site_id else report.site_ids
        spec = SOURCE_CONFIG[source]
        metrics = tuple(metric for metric in (spec["country_metric"], spec["region_metric"]) if metric)
        points = self.store.query(client_id=report.client_id, site_ids=site_ids,
            metric_ids=metrics, window=window)
        points = [point for point in points if point.source in {source, "fixture"}]
        actual_scopes = {(point.site_id, point.metric) for point in points if point.source == source}
        points = [point for point in points
            if point.source == source or (point.site_id, point.metric) not in actual_scopes]
        if spec["grain"] == "exact-window":
            points = [point for point in points if point.start == window.start and point.end == window.end]

        countries = defaultdict(Decimal); states = defaultdict(Decimal)
        observed_sites = set()
        for point in points:
            values = dict(point.dimensions); observed_sites.add(point.site_id)
            if point.metric == spec["country_metric"]:
                key = (values.get("country_code", ""), values.get("country_code_system", ""))
                if all(key): countries[key] += point.value
            elif point.metric == spec["region_metric"] and values.get("country_code") == "US":
                code = values.get("region_code", "").removeprefix("US-").upper()
                if not code and values.get("region_name"):
                    code = STATE_CODES_BY_NAME.get(values["region_name"].casefold(), "")
                if code in US_STATES: states[code] += point.value

        country_rows, withheld_countries = self._suppress(countries, country=True)
        state_rows, withheld_states = self._suppress(states, country=False)
        configured_sites = self._configured_sites(source, site_ids)
        return {
            "schema_version": 1,
            "source": source,
            "metric": spec["country_metric"],
            "label": spec["label"],
            "grain": spec["grain"],
            "window": {"start": window.start.isoformat(), "end": window.end.isoformat(), "end_exclusive": True},
            "site_ids": list(site_ids),
            "countries": country_rows,
            "us_states": state_rows,
            "region_support": {
                "status": "available" if spec["region_metric"] else "unavailable",
                "reason": None if spec["region_metric"] else f"{source} does not provide a state/region dimension in this connector.",
            },
            "counties": {
                "status": "unavailable",
                "reason": "Current providers do not expose trustworthy county aggregates. County boundaries are shown only for orientation; no values are inferred from city or IP data.",
            },
            "coverage": {
                "status": "observed" if observed_sites else "no_data",
                "configured_site_ids": configured_sites,
                "observed_site_ids": sorted(observed_sites),
                "note": "Observed rows are disclosed without claiming that omitted geography buckets are zero or complete.",
            },
            "suppression": {
                "threshold": self.suppression_threshold,
                "withheld_country_rows": withheld_countries,
                "withheld_us_state_rows": withheld_states,
            },
            "methodology": spec["methodology"],
        }

    def _configured_sites(self, source, site_ids):
        providers = {connection.id: connection.provider for connection in self.config.connections}
        return sorted({binding.site_id for binding in self.config.bindings
            if binding.site_id in site_ids and providers.get(binding.connection_id) == source})

    def _suppress(self, values, *, country):
        rows = []; withheld = 0
        for key, value in sorted(values.items(), key=lambda item: (-item[1], item[0])):
            if value < self.suppression_threshold:
                withheld += 1; continue
            if country:
                code, system = key
                rows.append({"code": code, "code_system": system, "value": _number(value)})
            else:
                rows.append({"code": key, "name": US_STATES[key], "value": _number(value)})
        return rows, withheld
