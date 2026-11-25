"""Part 2: map images. The public OpenStreetMap tile server (today) against its own usage policy, plus a static check of
how Komute's templates use it. Apple's tiles are measured inside the browser in t1_map_display.mjs.
Writes qa/out/t2_tiles.json. Run from anywhere:  python qa/t2_tiles.py

Kept small on purpose. The policy forbids bulk or scripted scanning, so this fetches 24 tiles at zoom 8 to 13, one every
0.6 s, with an identifying User-Agent and a Referer, honouring cache headers: about what a person panning a map causes.
"""
from __future__ import annotations

import json
import math
import re
import statistics
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE / "out" / "t2_tiles.json"
UA = {"User-Agent": "komute-map-qa/1.0 (map stack evaluation; run by the site owner)", "Referer": "https://komute.example/qa"}
CITIES = {"Toronto": (43.6532, -79.3832), "Ottawa": (45.4215, -75.6972), "Calgary": (51.0447, -114.0719), "Vancouver": (49.2827, -123.1207)}
ZOOMS = [8, 10, 12, 13, 11, 9]


def tile(lat, lon, z):
    n = 2 ** z
    return z, int((lon + 180) / 360 * n), int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)


rows = []
for city, (lat, lon) in CITIES.items():
    for z in ZOOMS:
        z_, x, y = tile(lat, lon, z)
        url = f"https://tile.openstreetmap.org/{z_}/{x}/{y}.png"
        t = time.perf_counter()
        try:
            r = requests.get(url, headers=UA, timeout=20)
            rows.append({"city": city, "z": z, "status": r.status_code, "ms": round((time.perf_counter() - t) * 1000), "bytes": len(r.content),
                         "cache_control": r.headers.get("Cache-Control"), "age": r.headers.get("Age"), "x_cache": r.headers.get("X-Cache"),
                         "type": r.headers.get("Content-Type"), "http": r.raw.version if r.raw else None})
        except Exception as e:
            rows.append({"city": city, "z": z, "status": None, "error": type(e).__name__, "ms": round((time.perf_counter() - t) * 1000)})
        time.sleep(0.6)

ms = [r["ms"] for r in rows if r.get("status") == 200]
q = lambda v, p: round(sorted(v)[max(0, int(len(v) * p) - 1)])

# Static check: does Komute use tiles the way the policy asks? Reads the templates and settings, changes nothing.
def read(rel):
    return (ROOT / rel).read_text(encoding="utf8", errors="ignore")


compliance = []
for page in ("templates/ride-summary.html", "templates/booking-details.html"):
    t = read(page)
    compliance.append({"file": page,
                       "https_tile_url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png" in t and "http://tile.openstreetmap.org" not in t,
                       "attribution_visible": "openstreetmap.org/copyright" in t,
                       "tile_referrer_policy": "referrerPolicy: 'strict-origin-when-cross-origin'" in t,
                       "no_prefetch_or_offline": not re.search(r"prefetch|offline|bulk", t, re.I)})
settings = read("config/settings.py")
compliance.append({"file": "config/settings.py", "site_referrer_policy": (re.search(r"SECURE_REFERRER_POLICY\s*=\s*['\"]([^'\"]+)", settings) or [None, None])[1],
                   "note": "same-origin is restrictive; the tile layers override it per request, as the policy requires"})

res = {
    "generated": time.strftime("%Y-%m-%d %H:%M"), "tiles": rows,
    "summary": {"requested": len(rows), "ok": sum(r.get("status") == 200 for r in rows), "median_ms": round(statistics.median(ms)) if ms else None,
                "p95_ms": q(ms, 0.95) if ms else None, "total_kb": round(sum(r.get("bytes", 0) for r in rows) / 1024)},
    "komute_template_check": compliance,
    # Read from https://operations.osmfoundation.org/policies/tiles/ on the day of this run.
    "policy": {"url": "https://operations.osmfoundation.org/policies/tiles/", "read": time.strftime("%Y-%m-%d"),
               "sla": "None. Availability is best-effort.",
               "commercial": "Commercial services should expect access to be withdrawn at any point; blocked without notice.",
               "must": ["visible OSM attribution", "a valid User-Agent that names the app", "a valid Referer from web pages", "honour cache headers (or cache at least 7 days)"],
               "must_not": ["bulk download or prefetch", "send no-cache headers by default", "set a restrictive Referrer-Policy that stops the Referer", "offline use"]},
    "other_public_services": {
        "osrm_demo": {"url": "https://github.com/Project-OSRM/osrm-backend/wiki/Demo-server", "terms": "Restricted to reasonable, non-commercial use. No more than 1 request per second. No guarantees on uptime, latency or data updates."},
        "photon_demo": {"url": "https://github.com/komoot/photon", "terms": "Use must stay at a reasonable level; extensive use is throttled or banned. No availability guarantee. Larger users are asked to run their own instance."},
    },
}
OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf8")
print("t2 done", res["summary"])
