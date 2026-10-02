# Fuel Route API

A Django REST API that, given a start and finish location in the USA, returns a drivable
route, the cost-minimal set of fuel stops along it (500-mile vehicle range, 10 mpg), the
total fuel purchase cost, and a rendered map.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # then fill in your OpenRouteService key
# .env uses the variable name `Api_key` (not ORS_API_KEY) - get a free key at
# https://openrouteservice.org/dev/#/signup (2000 directions/day, 1000 geocodes/day)

python manage.py migrate
python manage.py import_gazetteer      # downloads US Census Gazetteer place data (one-time, no API key)
python manage.py import_truckstops     # loads fuel-prices-for-be-assessment.csv, joins coordinates

python manage.py test                  # 29 tests, no network calls
python manage.py runserver
```

The committed `routing/data/gazetteer_lookup.json` means `import_gazetteer` only needs to
re-download if that file is missing or you want to refresh it (`--skip-download` reuses an
already-downloaded zip under `routing/data/`).

## Example request

```bash
curl -X POST http://127.0.0.1:8000/api/route/ \
  -H "Content-Type: application/json" \
  -d '{"start": "Chicago, IL", "finish": "Dallas, TX"}'
```

Also accepts `start_lat`/`start_lon`/`finish_lat`/`finish_lon` (skips geocoding entirely -
useful if you already have coordinates and want to stay under the external-call budget) and
an optional `initial_fuel_gallons` (0-50, defaults to a full tank).

Response includes `total_distance_miles`, `fuel_ledger` (consumption/purchases/cost), an
ordered `fuel_stops` array, a GeoJSON `map_data` FeatureCollection, and a `map_url` you can
open in a browser for a rendered Leaflet map of the route and stops. Full schema below.

## Architecture

```
fuel_route_api/        Django project (settings, urls)
routing/
  models.py             TruckStop
  views.py               POST /api/route/, GET /map/<key>/
  serializers.py
  services/
    geo_utils.py          haversine, bounding box, point-to-polyline projection
    geocoding.py           ORS live geocoding (start/finish), cached
    routing_client.py      ORS directions, cached
    fuel_optimizer.py      candidate discovery + fuel-ledger purchase algorithm
  management/commands/
    import_gazetteer.py    one-time: Census Gazetteer -> committed coordinate lookup
    import_truckstops.py   CSV -> DB, joins the lookup
  data/                    CSV, gazetteer zips/lookup (committed)
  tests/
```

## Geocoding strategy

The CSV has no coordinates, and its `Address` column is a highway/exit descriptor
(`"I-44, EXIT 283 & US-69"`), not a street address. Two tiers:

1. **US Census Bureau Gazetteer Files** (Places + County Subdivisions, free, no API key, no
   rate limit) are downloaded once and joined onto every row by `(city, state)` at import
   time - zero network calls at import, ~93% coverage of the CSV's unique city/state pairs
   (the rest are mostly Canadian provinces, plus a handful of US places the Gazetteer's
   "Places"/"County Subdivisions" layers don't carry). Rows with no match get
   `coordinate_precision="none"` and are excluded from routing until geocoded.
2. **Address-level upgrade via Nominatim was evaluated and rejected.** Before building it,
   I ran a timeboxed feasibility check: 25 random addresses, full descriptor string
   (`"I-44, EXIT 283 & US-69, Big Cabin, OK, USA"`) against Nominatim's structured search,
   1 req/sec. Result: **32% hit rate**, and several of the "hits" were actually *wrong* -
   e.g. `"SR-71 & CR-214, Cedar Creek, TX"` matched to a street in Dallas, not Cedar Creek.
   Highway/exit descriptors aren't addresses a geocoder can parse reliably. Shipping that
   pass would have replaced a reliable city centroid with an unreliable, sometimes-wrong
   guess for ~1 in 3 rows. **Decision: skip it.** City-level `coordinate_precision="city"`
   is the floor for every located row, and the API reports that precision per stop rather
   than implying exact station coordinates.

Live, per-request geocoding of the user-supplied `start`/`finish` text uses
OpenRouteService - this is the piece that counts against the assessment's "1-3 external
calls per request" budget.

## Algorithm

**Candidate discovery:** route geometry (from ORS) is turned into cumulative-distance
segments, rescaled so they sum exactly to ORS's own authoritative total distance (the raw
haversine sum of a road-network polyline and the reported road distance aren't identical -
rescaling keeps per-station "distance from start" consistent with the headline trip
distance). A bounding-box query against `TruckStop.latitude/longitude` cuts ~8000 rows down
to a small candidate set before any per-candidate projection math runs. Each candidate is
projected onto the nearest route segment for its perpendicular off-route distance and its
position along the route. Primary corridor: 20 miles; if a route has zero primary-corridor
candidates anywhere a plan needs one, the whole search retries once against a 50-mile
fallback corridor.

This is a **straight-line corridor distance, not a verified driving detour** - the API
field is named `off_route_straight_line_miles` and the response carries
`route_validation: "unvalidated_corridor_estimate"` rather than implying a verified route
through each stop (verifying that would cost a 4th external call per stop or per route,
which is out of the "1-3 calls" budget - a deliberate, documented tradeoff).

**Fuel purchase algorithm:** a fuel ledger (gallons in tank) is tracked explicitly through
every candidate station in route order. At each station: if a strictly cheaper station
exists within one full tank's range ahead, buy just enough to reach it; otherwise fill up,
capped by whatever's needed to finish the trip. This is the classical optimal greedy for a
fixed-path, tank-capacity-constrained, continuous-purchase refueling problem - provably
optimal under that model, no DP needed, and it matches the brief's "optimal mostly means
cost-effective." Verified against a test-only exhaustive brute-force DP oracle on 40 random
small integer-unit instances (`routing/tests/test_fuel_optimizer.py`) - greedy cost matches
the oracle optimum exactly on every feasible case, and agrees on every infeasible one.

If a gap between reachable candidates exceeds the vehicle's range even at the fallback
corridor, the API returns `"status": "NO_FEASIBLE_FUEL_PLAN"` (HTTP 422) with a message
that says "no feasible plan with the *available station data*" - not "no station exists" -
since the gap may just be a hole in the corridor-filtered candidate set, not reality. The
base route and partial plan are still returned for explanation.

## Assumptions & scope

- **10 mpg, 50-gallon tank (500-mile range), full tank at departure** (overridable via
  `initial_fuel_gallons`). The brief doesn't specify whether the initial tank's fuel has a
  cost basis - it's treated as free/pre-trip, so `fuel_purchase_cost_usd` only reflects
  purchases made *during* the trip (`cost_basis` field makes this explicit). A trip at or
  under 500 miles therefore shows `0` additional purchases while still reporting nonzero
  fuel *consumption* - these are reported as separate fields, not conflated.
- Purchase algorithm is the classical greedy described above, not a DP - correct under the
  fixed-route/full-information/continuous-purchase model, not accounting for detour cost or
  real-world station supply limits.
- Candidate distance to the route is straight-line, not a verified driving detour (see
  above).
- USA scope is validated via the geocoder's returned country/region metadata, not a
  hardcoded CONUS bounding box - so Alaska/Hawaii aren't pre-emptively blocked (a route ORS
  genuinely can't drive, e.g. Hawaii-to-mainland, surfaces as its own routing error rather
  than being filtered out in advance).
- Coordinate precision is city-level for ~93% of rows (US Census Gazetteer); the remaining
  rows (mostly Canadian-province entries plus a handful of ungazetteered US places) are
  unlocated and excluded from routing. An address-level upgrade pass was evaluated and
  rejected for unreliability - see Geocoding strategy above.
- Route geometry nearest-point projection takes the global minimum across all segments,
  which could misbehave on a route that loops near itself. Low-probability for point-to-point
  interstate trips (the primary use case here) and not specifically engineered around.
- Money fields use `Decimal` (storage and final cost arithmetic); physical-distance math
  (haversine, projection) stays `float` - nobody does `Decimal` trigonometry, and distance
  isn't currency.

## External call budget

| Scenario | ORS calls |
|---|---|
| Fresh start + fresh finish (place names) | 2 geocodes + 1 directions = **3** |
| One endpoint already cached | **2** |
| Both cached, or coordinates supplied directly | **0-1** |
| Fully cached repeat request | **0** |

Geocode results are cached by normalized input text; route results by resolved coordinate
pair; both via Django's cache framework, 24h TTL. The response includes
`external_calls_made` so this is measurable per-request, not just asserted. `requests` is
used with no retry adapter and explicit connect/read timeouts, so a "call" is exactly one
HTTP attempt. We deliberately do **not** make a 4th call to verify a route waypointed
through the chosen stops (see Algorithm, above) - reflected via `route_validation` rather
than silently promising a validation that wasn't done.

## Error handling

- `400` - missing/blank `start`/`finish`, identical `start`/`finish`, invalid
  `initial_fuel_gallons`.
- `422` - geocoding failed or resolved outside the US, ORS reports a route beyond its
  distance limits, or `NO_FEASIBLE_FUEL_PLAN`.
- `502` - malformed upstream response. `503` - ORS quota/rate-limit. `504` - upstream
  timeout.

## Loom video

Suggested sequence (≤5 min): repo/code walkthrough -> why Gazetteer-first geocoding (and
why the Nominatim address pass was rejected, with the 32% hit-rate evidence) -> fuel ledger
+ worked-example walkthrough -> live Postman demo (short trip, long trip) -> open `map_url`
-> `external_calls_made` shown live -> test suite.
