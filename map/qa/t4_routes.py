"""Part 4: routes, distance and drive time. OSRM public demo (today) vs Apple Directions (the map node).
Writes qa/out/t4_routes.json. Run from anywhere:  python qa/t4_routes.py   (about 25 Apple calls; OSRM is called once a second)
"""
from __future__ import annotations

import json
import math
import statistics
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.core.config import Settings

OUT = HERE / "out" / "t4_routes.json"
UA = {"User-Agent": "komute-map-qa/1.0"}
app = TestClient(create_app(Settings(_env_file=HERE.parents[1] / ".env", api_key=None)))

P = {  # name: (lat, lon). City halls and landmarks, coordinates from the t3 geocode checks.
    "Toronto City Hall": (43.6528, -79.3836), "CN Tower": (43.6426, -79.3871), "Mississauga Civic Centre": (43.5890, -79.6441),
    "Parliament Hill, Ottawa": (45.4236, -75.7009), "Gatineau (Maison du citoyen)": (45.4263, -75.7105), "Kingston City Hall": (44.2312, -76.4860),
    "McGill, Montréal": (45.5048, -73.5772), "Québec City (Assemblée nationale)": (46.8086, -71.2145), "Calgary City Hall": (51.0447, -114.0577),
    "Edmonton City Hall": (53.5444, -113.4902), "Vancouver Art Gallery": (49.2828, -123.1207), "Canada Place, Vancouver": (49.2889, -123.1111),
}
ROUTES = [
    ("Toronto City Hall", "Parliament Hill, Ottawa", "intercity"), ("McGill, Montréal", "Québec City (Assemblée nationale)", "intercity"),
    ("Calgary City Hall", "Edmonton City Hall", "intercity"), ("Mississauga Civic Centre", "Toronto City Hall", "commute"),
    ("Toronto City Hall", "CN Tower", "short, downtown"), ("Vancouver Art Gallery", "Canada Place, Vancouver", "short, downtown"),
    ("Parliament Hill, Ottawa", "Gatineau (Maison du citoyen)", "across the river"),
]
RIDERS = [("Kingston City Hall", "on the way"), ("Mississauga Civic Centre", "wrong direction"), ("Gatineau (Maison du citoyen)", "near the end")]
MS: dict[str, list[float]] = {"apple_route": [], "apple_matrix": [], "osrm_route": []}


def hav_m(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


def seg_dist_m(p, a, b) -> float:
    """Distance from point p to segment a-b, in metres (flat-earth is fine at these scales)."""
    k = 111320
    cl = math.cos(math.radians(p[0]))
    ax, ay, bx, by, px, py = a[1] * k * cl, a[0] * k, b[1] * k * cl, b[0] * k, p[1] * k * cl, p[0] * k
    dx, dy = bx - ax, by - ay
    t = 0 if dx == dy == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def line_gap_m(line_a, line_b, samples=60):
    """How far line A strays from line B: max and median over evenly spaced points on A. Lines are [[lon, lat], ...]."""
    if len(line_a) < 2 or len(line_b) < 2:
        return None
    idx = [round(i * (len(line_a) - 1) / (samples - 1)) for i in range(samples)]
    out = []
    for i in idx:
        p = (line_a[i][1], line_a[i][0])
        out.append(min(seg_dist_m(p, (line_b[j][1], line_b[j][0]), (line_b[j + 1][1], line_b[j + 1][0])) for j in range(len(line_b) - 1)))
    return {"max_m": round(max(out)), "median_m": round(statistics.median(out)), "within_200m_pct": round(100 * sum(d <= 200 for d in out) / len(out))}


def apple_route(a, b, waypoints=()):
    t = time.perf_counter()
    r = app.post("/v1/route", json={"origin": {"lat": a[0], "lon": a[1]}, "destination": {"lat": b[0], "lon": b[1]}, "waypoints": [{"lat": w[0], "lon": w[1]} for w in waypoints]})
    MS["apple_route"].append((time.perf_counter() - t) * 1000)
    r.raise_for_status()
    return r.json()


def osrm(points):
    coords = ";".join(f"{p[1]},{p[0]}" for p in points)
    t = time.perf_counter()
    r = requests.get(f"https://router.project-osrm.org/route/v1/driving/{coords}?overview=full&geometries=geojson", headers=UA, timeout=30)
    MS["osrm_route"].append((time.perf_counter() - t) * 1000)
    time.sleep(1.0)
    rt = r.json()["routes"][0]
    return {"km": rt["distance"] / 1000, "min": rt["duration"] / 60, "line": rt["geometry"]["coordinates"]}


res = {"generated": time.strftime("%Y-%m-%d %H:%M"), "routes": [], "detour": [], "stops": None}
for a, b, kind in ROUTES:
    A, B = P[a], P[b]
    ap, os_ = apple_route(A, B), osrm([A, B])
    res["routes"].append({
        "from": a, "to": b, "kind": kind, "straight_km": round(hav_m(A, B) / 1000, 1),
        "apple_km": round(ap["distance_m"] / 1000, 1), "apple_min": round(ap["duration_s"] / 60, 1), "apple_points": len(ap["geometry"]),
        "osrm_km": round(os_["km"], 1), "osrm_min": round(os_["min"], 1), "osrm_points": len(os_["line"]),
        "km_diff_pct": round((ap["distance_m"] / 1000 - os_["km"]) / os_["km"] * 100, 1), "min_diff_pct": round((ap["duration_s"] / 60 - os_["min"]) / os_["min"] * 100, 1),
        "line_gap": line_gap_m(ap["geometry"], os_["line"]),
    })

# a ride with a stop, as post-ride builds it: Toronto -> Kingston -> Ottawa
A, S, B = P["Toronto City Hall"], P["Kingston City Hall"], P["Parliament Hill, Ottawa"]
ap, os_ = apple_route(A, B, [S]), osrm([A, S, B])
res["stops"] = {"route": "Toronto City Hall > Kingston > Ottawa", "apple_km": round(ap["distance_m"] / 1000, 1), "apple_min": round(ap["duration_s"] / 60, 1),
                "apple_legs": len(ap["legs"]), "apple_calls": len(ap["legs"]), "osrm_km": round(os_["km"], 1), "osrm_min": round(os_["min"], 1),
                "osrm_calls": 1, "line_gap": line_gap_m(ap["geometry"], os_["line"])}

# driver detour, with the node's two methods, against the 30 km rule (STOP_DETOUR_MAX_KM) in komute/settings
direct = apple_route(A, B)
pts = [A] + [P[n] for n, _ in RIDERS] + [B]
t = time.perf_counter()
mx = app.post("/v1/matrix", json={"origins": [{"lat": p[0], "lon": p[1]} for p in pts[:-1]], "destinations": [{"lat": p[0], "lon": p[1]} for p in pts[1:]]}).json()["rows"]
MS["apple_matrix"].append((time.perf_counter() - t) * 1000)
for k, (n, why) in enumerate(RIDERS):
    d_ap, d_pb = mx[0][k], mx[k + 1][3]
    via = apple_route(A, B, [P[n]])
    m_km = None if not (d_ap and d_pb) else round((d_ap["distance_m"] + d_pb["distance_m"] - direct["distance_m"]) / 1000, 1)
    m_min = None if not (d_ap and d_pb) else round((d_ap["duration_s"] + d_pb["duration_s"] - direct["duration_s"]) / 60, 1)
    r_km, r_min = round((via["distance_m"] - direct["distance_m"]) / 1000, 1), round((via["duration_s"] - direct["duration_s"]) / 60, 1)
    o_via = osrm([A, P[n], B])
    o_dir = osrm([A, B])
    res["detour"].append({"rider": n, "why": why, "matrix_km": m_km, "matrix_min": m_min, "route_km": r_km, "route_min": r_min,
                          "osrm_km": round(o_via["km"] - o_dir["km"], 1), "osrm_min": round(o_via["min"] - o_dir["min"], 1),
                          "over_30km_apple": bool((m_km or 0) > 30), "methods_disagree_km": None if m_km is None else round(abs(m_km - r_km), 1)})

q = lambda v, p: round(sorted(v)[max(0, int(len(v) * p) - 1)])
res["latency_ms"] = {k: {"n": len(v), "median": round(statistics.median(v)), "p95": q(v, 0.95)} for k, v in MS.items() if v}
OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf8")
print("t4 done", {k: v["n"] for k, v in res["latency_ms"].items()})
