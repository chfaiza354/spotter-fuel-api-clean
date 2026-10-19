"""Core logic: geocoding (Nominatim), routing (OSRM) and the fuel-stop optimizer.

Free APIs used (no API keys needed):
  * Nominatim (OpenStreetMap) - forward & reverse geocoding
  * OSRM public demo server - driving route + distance + GeoJSON geometry

External calls are minimized:
  * OSRM is called exactly ONCE per request.
  * Every Nominatim result is cached in the DB (GeocodeCache) and every
    truck stop's coordinates are cached on the TruckStop row, so repeat
    requests for the same places hit no external API at all.
  * Nominatim requests are throttled to ~1/second per its usage policy.
"""
import math
import threading
import time

import requests

from fuel.models import GeocodeCache, TruckStop

# ---------------------------------------------------------------------------
# Vehicle constants (from the assessment brief)
# ---------------------------------------------------------------------------
MPG = 10               # miles per gallon
TANK_GALLONS = 50      # full tank
MAX_RANGE_MILES = MPG * TANK_GALLONS  # 500 miles

# Full state name -> USPS abbreviation (Nominatim returns full names,
# the OPIS price list uses abbreviations).
US_STATE_ABBR = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT",
    "Delaware": "DE", "District of Columbia": "DC", "Florida": "FL",
    "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL",
    "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY",
    "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
    "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH",
    "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH",
    "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD",
    "Tennessee": "TN", "Texas": "TX", "Utah": "UT", "Vermont": "VT",
    "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY",
    "Puerto Rico": "PR", "Guam": "GU",
}


def state_abbr(name):
    """Normalize a state name (or abbreviation) to the 2-letter code."""
    name = (name or "").strip()
    if len(name) == 2:
        return name.upper()
    return US_STATE_ABBR.get(name, name)

# How often (in miles) to look for a fuel-stop candidate along the route.
# 200 < 500 means we always have a comfortable margin even if one candidate
# point yields no station.
STOP_SPACING_MILES = 200

NOMINATIM_URL = "https://nominatim.openstreetmap.org"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
USER_AGENT = "SpotterFuelAPI/1.0 (coding-assessment; contact: spotter-assessment)"

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})

# Nominatim asks for at most 1 request/second.
_throttle_lock = threading.Lock()
_last_nominatim_call = [0.0]


def _throttled_get(url, params):
    with _throttle_lock:
        wait = 1.1 - (time.time() - _last_nominatim_call[0])
        if wait > 0:
            time.sleep(wait)
        resp = _session.get(url, params=params, timeout=30)
        _last_nominatim_call[0] = time.time()
    resp.raise_for_status()
    return resp.json()


def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles between two lat/lon points."""
    r = 3958.8  # Earth radius in miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Geocoding (cached)
# ---------------------------------------------------------------------------
def geocode(query):
    """Return (lat, lon, display_name) for a place name. Cached in the DB."""
    cached = GeocodeCache.objects.filter(query=f"geo:{query}").first()
    if cached:
        return cached.latitude, cached.longitude, cached.display_name

    data = _throttled_get(f"{NOMINATIM_URL}/search",
                          {"q": query, "format": "json", "limit": 1,
                           "countrycodes": "us", "addressdetails": 1})
    if not data:
        raise ValueError(f"Could not geocode location: {query!r}")
    hit = data[0]
    lat, lon = float(hit["lat"]), float(hit["lon"])
    addr = hit.get("address", {})
    GeocodeCache.objects.create(
        query=f"geo:{query}", latitude=lat, longitude=lon,
        display_name=hit.get("display_name", ""),
        city=addr.get("city") or addr.get("town") or addr.get("village") or "",
        state=addr.get("state") or "",
    )
    return lat, lon, hit.get("display_name", "")


def reverse_geocode_state(lat, lon):
    """Return the 2-letter state abbreviation for coordinates.

    Uses a coarse cache key (2 decimals ~ 1 km grid) so tiny route-geometry
    variations between requests still hit the cache. zoom=5 asks Nominatim
    for state-level detail only.
    """
    key = f"revs:{lat:.2f},{lon:.2f}"
    cached = GeocodeCache.objects.filter(query=key).first()
    if cached:
        return cached.state

    data = _throttled_get(f"{NOMINATIM_URL}/reverse",
                          {"lat": lat, "lon": lon, "format": "json",
                           "zoom": 5, "addressdetails": 1})
    addr = (data or {}).get("address", {})
    state = state_abbr(addr.get("state") or "")
    GeocodeCache.objects.create(
        query=key, latitude=lat, longitude=lon,
        display_name=(data or {}).get("display_name", ""),
        state=state,
    )
    return state


def geocode_station(station):
    """Geocode a truck stop (cached on the row). Returns (lat, lon) or None.

    Tries the full street address first (most accurate); falls back to the
    city center, because highway-exit addresses like "I-80, EXIT 143" are
    often not in Nominatim's index. Failures are flagged on the row so they
    are never retried.
    """
    if station.latitude is not None and station.longitude is not None:
        return station.latitude, station.longitude
    if station.geocode_failed:
        return None
    queries = []
    if station.address:
        queries.append(f"{station.address}, {station.city}, {station.state}, USA")
    queries.append(f"{station.city}, {station.state}, USA")  # city-center fallback
    for query in queries:
        try:
            lat, lon, _ = geocode(query)
        except (ValueError, requests.RequestException):
            continue
        station.latitude, station.longitude = lat, lon
        station.save(update_fields=["latitude", "longitude"])
        return lat, lon
    station.geocode_failed = True
    station.save(update_fields=["geocode_failed"])
    return None


# ---------------------------------------------------------------------------
# Routing (exactly one OSRM call per request)
# ---------------------------------------------------------------------------
def get_route(lat1, lon1, lat2, lon2):
    """Return (distance_miles, duration_hours, coordinates[[lon, lat], ...])."""
    url = f"{OSRM_URL}/{lon1},{lat1};{lon2},{lat2}"
    resp = _session.get(url, params={"overview": "full", "geometries": "geojson"},
                        timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        raise ValueError("No driving route found between these locations.")
    route = data["routes"][0]
    distance_miles = route["distance"] * 0.000621371
    duration_hours = route["duration"] / 3600.0
    coords = route["geometry"]["coordinates"]  # [lon, lat] pairs
    return distance_miles, duration_hours, coords


def walk_route(coords, spacing_miles):
    """Return [(distance_from_start_miles, lat, lon)] every `spacing` miles."""
    points = []
    acc = 0.0
    prev_lon, prev_lat = coords[0]
    for lon, lat in coords[1:]:
        seg = haversine_miles(prev_lat, prev_lon, lat, lon)
        # Emit evenly spaced markers inside this segment.
        while acc + seg >= (len(points) + 1) * spacing_miles:
            target = (len(points) + 1) * spacing_miles
            frac = (target - acc) / seg if seg else 0
            m_lon = prev_lon + frac * (lon - prev_lon)
            m_lat = prev_lat + frac * (lat - prev_lat)
            points.append((target, m_lat, m_lon))
        acc += seg
        prev_lon, prev_lat = lon, lat
    return points


# ---------------------------------------------------------------------------
# Station lookup near a route point
# ---------------------------------------------------------------------------
def find_cheapest_station_near(lat, lon):
    """Find a cheap truck stop near (lat, lon). Returns TruckStop or None.

    The candidate set - the cheapest stations in the state - does NOT depend
    on the exact marker position, so geocoding results stay cached even if
    the routing geometry shifts slightly between requests. The final pick is
    the cheapest candidate within 60 miles of the marker (pure math, no API
    calls), falling back to the nearest cheap candidate in sparse areas.
    """
    state = reverse_geocode_state(lat, lon)
    if not state:
        return None

    def cheapest_near(candidates, max_miles):
        """Cheapest candidate within max_miles of the marker (price first)."""
        for st in candidates:  # already ordered cheapest-first
            pos = geocode_station(st)
            if not pos:
                continue
            if haversine_miles(lat, lon, pos[0], pos[1]) <= max_miles:
                return st
        return None

    def nearest(candidates):
        best, best_d = None, float("inf")
        for st in candidates:
            pos = geocode_station(st)
            if not pos:
                continue
            d = haversine_miles(lat, lon, pos[0], pos[1])
            if d < best_d:
                best, best_d = st, d
        return best

    qs = TruckStop.objects.filter(state__iexact=state) \
        .order_by("retail_price", "opis_id")  # opis_id breaks price ties deterministically
    hit = cheapest_near(qs[:15], 60)
    if hit is None:
        # Sparse area: nearest of the cheapest 60 in the state.
        hit = nearest(qs[:60])
    return hit


# ---------------------------------------------------------------------------
# Minimum-cost fueling plan (greedy - optimal for varying gas prices)
# ---------------------------------------------------------------------------
def plan_fuel(stops):
    """Compute gallons to buy at each stop for minimum total cost.

    `stops`: list of dicts with keys distance (miles from start), price
    ($/gal), and station info. Assumes the vehicle leaves the start with a
    full tank (50 gal). At each stop, look ahead within the 500-mile range:
    if a cheaper station is reachable, buy only enough to get there,
    otherwise fill the tank. Returns the stops annotated with gallons/cost,
    plus totals.
    """
    fuel_in_tank = TANK_GALLONS
    total_gallons = 0.0
    total_cost = 0.0
    plan = []

    for i, stop in enumerate(stops):
        # Cheapest price reachable ahead within one tank.
        future = [s for s in stops[i + 1:]
                  if s["distance"] - stop["distance"] <= MAX_RANGE_MILES]
        cheaper_ahead = [s for s in future if s["price"] < stop["price"]]

        # Never buy more than the fuel still needed to finish the trip.
        trip_remaining = (stops[-1]["distance"] - stop["distance"]) / MPG
        if cheaper_ahead:
            # Buy just enough to reach the nearest cheaper station.
            target = min(cheaper_ahead, key=lambda s: s["distance"])
            need = (target["distance"] - stop["distance"]) / MPG
        else:
            # No cheaper fuel ahead: fill up (but no more than the trip needs).
            need = TANK_GALLONS
        need = min(need, trip_remaining)

        buy = max(0.0, min(need - fuel_in_tank, TANK_GALLONS - fuel_in_tank))
        # Round to cents-friendly 2 decimals for display; keep math simple.
        buy = round(buy, 2)
        if stop.get("_anchor") == "finish":
            buy = 0.0  # never purchase fuel at the destination
        cost = round(buy * stop["price"], 2)
        fuel_in_tank = round(fuel_in_tank + buy, 2)

        plan.append({**stop, "gallons": buy, "cost": cost})
        total_gallons += buy
        total_cost += cost

        # Drive to the next stop (or finish).
        next_d = stops[i + 1]["distance"] if i + 1 < len(stops) else None
        if next_d is not None:
            fuel_in_tank = max(0.0,
                round(fuel_in_tank - (next_d - stop["distance"]) / MPG, 2))

    return plan, round(total_gallons, 2), round(total_cost, 2)


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------
def compute_route_plan(start_query, finish_query):
    """Full pipeline: geocode -> route -> stations -> fuel plan."""
    s_lat, s_lon, s_name = geocode(start_query)
    f_lat, f_lon, f_name = geocode(finish_query)

    distance_miles, duration_hours, coords = get_route(s_lat, s_lon, f_lat, f_lon)

    # Candidate stop points along the route.
    markers = walk_route(coords, STOP_SPACING_MILES)

    stations = []
    seen = set()
    for target_d, m_lat, m_lon in markers:
        if target_d >= distance_miles - 20:
            # Too close to the destination to be useful.
            continue
        st = find_cheapest_station_near(m_lat, m_lon)
        if st is None or st.opis_id in seen:
            continue
        seen.add(st.opis_id)
        stations.append({
            "distance": round(target_d, 1),
            "price": float(st.retail_price),
            "name": st.name,
            "address": st.address,
            "city": st.city,
            "state": st.state,
            "latitude": st.latitude,
            "longitude": st.longitude,
        })

    stations.sort(key=lambda s: s["distance"])

    # Anchor the plan with the start (full 50-gal tank, fuel already paid for)
    # and the finish (no fueling possible). plan_fuel() needs both to make
    # correct buy/don't-buy decisions at the last real stop.
    anchors = (
        [{"distance": 0.0, "price": 0.0, "name": "Start - full tank",
          "address": "", "city": "", "state": "",
          "latitude": s_lat, "longitude": s_lon, "_anchor": "start"}]
        + stations
        + [{"distance": round(distance_miles, 1), "price": 9999.0,  # sentinel: never buy here
            "name": "Destination", "address": "", "city": "", "state": "",
            "latitude": f_lat, "longitude": f_lon, "_anchor": "finish"}]
    )

    # Verify reachability: every leg must be within tank range.
    legs = [a["distance"] for a in anchors]
    max_leg = max(b - a for a, b in zip(legs, legs[1:]))

    full_plan, total_gallons, total_cost = plan_fuel(anchors)
    # Only real fuel stops go into the response: drop the anchors and any
    # stop where the optimal plan buys nothing.
    fuel_stops = [{k: v for k, v in s.items() if not k.startswith("_")}
                  for s in full_plan
                  if "_anchor" not in s and s["gallons"] > 0]

    return {
        "start": {"query": start_query, "latitude": s_lat, "longitude": s_lon,
                  "display_name": s_name},
        "finish": {"query": finish_query, "latitude": f_lat, "longitude": f_lon,
                   "display_name": f_name},
        "route": {
            "distance_miles": round(distance_miles, 1),
            "duration_hours": round(duration_hours, 1),
            "max_leg_miles": round(max_leg, 1),
            "geometry": {"type": "LineString", "coordinates": coords},
        },
        "fuel_stops": fuel_stops,
        "total_gallons": total_gallons,
        "total_fuel_cost": total_cost,
        "mpg": MPG,
        "tank_gallons": TANK_GALLONS,
        "max_range_miles": MAX_RANGE_MILES,
    }
