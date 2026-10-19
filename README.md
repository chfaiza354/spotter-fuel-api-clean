# Spotter Fuel API — Coding Assessment

Django REST API that plans the cheapest-fuel driving route between two US locations.

**Endpoint:** `POST /api/route/`

```json
{ "start": "Chicago, IL", "finish": "Denver, CO" }
```

Returns the route (distance, duration, GeoJSON map geometry), the optimal
fuel stops with per-gallon prices, gallons to buy at each stop, and the total
fuel cost.

## Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cd spotter_fuel_api
python manage.py migrate
python manage.py load_fuel_prices   # loads the 6,738 truck stops from the CSV
python manage.py runserver
```

Then:

```bash
curl -X POST http://127.0.0.1:8000/api/route/ \
  -H "Content-Type: application/json" \
  -d '{"start": "Chicago, IL", "finish": "Denver, CO"}'
```

## How it works

1. **Geocode** start/finish with Nominatim (OpenStreetMap), cached in the DB.
2. **Route** with OSRM — exactly **one** routing call per request. Returns
   distance, duration and full GeoJSON geometry for the map.
3. **Candidate stops:** walk along the route geometry and drop a candidate
   marker every 200 miles (well under the 500-mile tank range, so there is
   always margin). At each marker, look up the state, take the cheapest
   truck stops in that state from the price list, and pick the nearest one.
4. **Fueling plan (greedy, optimal):** the vehicle starts with a full
   50-gallon tank (10 MPG × 50 gal = 500-mile range). At each stop, look
   ahead within one tank of range: if a cheaper station is reachable, buy
   only enough to get there; otherwise fill up — but never more than the
   fuel still needed to finish the trip. This greedy strategy is optimal for
   varying gas prices.
5. **Total cost** = Σ (gallons × price) over the stops.

## Free APIs used (no keys needed)

| API | Use | Calls per request |
|---|---|---|
| Nominatim (`nominatim.openstreetmap.org`) | forward/reverse geocoding | cached in DB; throttled to ~1/sec per usage policy |
| OSRM (`router.project-osrm.org`) | driving route + GeoJSON | exactly 1 |

Repeat requests for the same places hit only the cache: 1 OSRM call and
(usually) zero Nominatim calls, answering in ~1 second. A brand-new route
takes longer on its first run while station coordinates are geocoded and
cached.

## Assumptions

- Vehicle starts the trip with a **full tank** (fuel already paid for — not
  counted in the total); only fuel bought en route is costed.
- 10 miles/gallon, 50-gallon tank, 500-mile max range (per the brief).
- Prices come from the provided OPIS truck-stop list (deduplicated by
  station, keeping each station's lowest listed price).
- Station locations are geocoded from their street address + city + state;
  the stop's position on the route is approximated by the candidate marker.

## Project layout

```
spotter_fuel_api/
├── spotter_fuel_api/      # project: settings, urls
└── fuel/
    ├── models.py          # TruckStop, GeocodeCache
    ├── services.py        # geocoding, routing, fuel-stop optimizer
    ├── views.py           # POST /api/route/
    └── management/commands/load_fuel_prices.py
requirements.txt
```

Built with Django 6.1 + Django REST Framework, SQLite.
