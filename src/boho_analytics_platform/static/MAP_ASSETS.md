# Geographic boundary assets

The dashboard serves these files locally. It does not contact a map or analytics CDN at runtime.

- `natural-earth-countries-110m.geojson`: Natural Earth Vector v5.1.2,
  `geojson/ne_110m_admin_0_countries.geojson`. Natural Earth data is public domain.
  Source: https://github.com/nvkelso/natural-earth-vector/tree/v5.1.2
  SHA-256: `6866c877d39cba9c357620878839b336d569f8c662d3cfab4cb1dbe2d39c977f`.
- `us-counties-albers-10m.json`: US Atlas v3.0.1,
  `counties-albers-10m.json`. US Atlas is ISC licensed and derives these boundaries
  from US Census Bureau cartographic boundary files.
  Source package: https://www.jsdelivr.com/package/npm/us-atlas (version 3.0.1).
  Upstream: https://github.com/topojson/us-atlas/tree/v3.0.1
  SHA-256: `a674dfa31b625e92635f32684a61ae135b05a91507f17f2d5164833237fecf46`.
  The upstream ISC terms are retained in `US_ATLAS_LICENSE.txt`.

The US file contains nation, state, and county boundaries. The dashboard currently
uses county geometry only for orientation. It does not infer county-level visitor
values from cities, coordinates, IP addresses, or any other proxy.
