"""Part 3: address search and "Locate Me". Photon (today) vs Apple Maps (the map node), with OpenStreetMap's Nominatim as a
third, independent reference. Writes qa/out/t3_search.json. Run from anywhere:  python qa/t3_search.py
About 200 calls to the Apple allowance. Public Photon and Nominatim are called politely (Nominatim: 1 request per second).
"""
from __future__ import annotations

import json
import math
import re
import statistics
import sys
import time
import unicodedata
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.core.config import Settings

OUT = HERE / "out" / "t3_search.json"
UA = {"User-Agent": "komute-map-qa/1.0"}
app = TestClient(create_app(Settings(_env_file=HERE.parents[1] / ".env", api_key=None)))
MS: dict[str, list[float]] = {}


def timed(tag, fn):
    t = time.perf_counter()
    try:
        return fn()
    finally:
        MS.setdefault(tag, []).append((time.perf_counter() - t) * 1000)


# ---- matching: street words are normalised so "Street" = "St", "Third" = "3rd", "Ouest" = "O" ---------------------------
ABBR = {"street": "st", "avenue": "ave", "road": "rd", "drive": "dr", "boulevard": "blvd", "place": "pl", "square": "sq", "court": "ct",
        "west": "w", "east": "e", "north": "n", "south": "s", "ouest": "o", "est": "e", "nord": "n", "sud": "s", "saint": "st", "sainte": "ste",
        "first": "1st", "second": "2nd", "third": "3rd", "fourth": "4th", "fifth": "5th"}


def norm(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().casefold()
    return " ".join(ABBR.get(w, w) for w in re.sub(r"[^a-z0-9 ]+", " ", s).split())


def has_words(text: str | None, words: str) -> bool:
    have = set(norm(text).split())
    return all(w in have for w in norm(words).split())


def hav_m(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))


# name, typed text, expected house number, street words, city words
ADDR = [
    ("Toronto City Hall", "100 Queen Street West, Toronto, ON", "100", "queen", "toronto"),
    ("CN Tower", "301 Front Street West, Toronto, ON", "301", "front", "toronto"),
    ("Union Station", "65 Front Street West, Toronto, ON", "65", "front", "toronto"),
    ("Mississauga Civic Centre", "300 City Centre Drive, Mississauga, ON", "300", "city centre", "mississauga"),
    ("Toronto Zoo", "2000 Meadowvale Road, Toronto, ON", "2000", "meadowvale", "toronto scarborough"),
    ("Parliament Hill, Ottawa", "111 Wellington Street, Ottawa, ON", "111", "wellington", "ottawa"),
    ("Gatineau (Maison du citoyen)", "25 Rue Laurier, Gatineau, QC", "25", "laurier", "gatineau"),
    ("McGill, Montréal", "845 Rue Sherbrooke Ouest, Montréal, QC", "845", "sherbrooke", "montreal"),
    ("Hôtel de ville, Montréal", "275 Rue Notre-Dame Est, Montréal, QC", "275", "notre dame", "montreal"),
    ("Assemblée nationale, Québec", "1045 Rue des Parlementaires, Québec, QC", "1045", "parlementaires", "quebec"),
    ("Château Frontenac, Québec", "1 Rue des Carrières, Québec, QC", "1", "carrieres", "quebec"),
    ("Vancouver Art Gallery", "750 Hornby Street, Vancouver, BC", "750", "hornby", "vancouver"),
    ("Canada Place, Vancouver", "999 Canada Place, Vancouver, BC", "999", "canada", "vancouver"),
    ("Centennial Square, Victoria", "1 Centennial Square, Victoria, BC", "1", "centennial", "victoria"),
    ("Calgary City Hall", "800 Macleod Trail SE, Calgary, AB", "800", "macleod", "calgary"),
    ("Edmonton City Hall", "1 Sir Winston Churchill Square, Edmonton, AB", "1", "churchill", "edmonton"),
    ("Saskatoon City Hall", "222 3rd Avenue North, Saskatoon, SK", "222", "3rd", "saskatoon"),
    ("Regina City Hall", "2476 Victoria Avenue, Regina, SK", "2476", "victoria", "regina"),
    ("Winnipeg City Hall", "510 Main Street, Winnipeg, MB", "510", "main", "winnipeg"),
    ("Kingston City Hall", "216 Ontario Street, Kingston, ON", "216", "ontario", "kingston"),
    ("Halifax City Hall", "1841 Argyle Street, Halifax, NS", "1841", "argyle", "halifax"),
    ("Fredericton City Hall", "397 Queen Street, Fredericton, NB", "397", "queen", "fredericton"),
    ("Charlottetown City Hall", "199 Queen Street, Charlottetown, PE", "199", "queen", "charlottetown"),
    ("St. John's City Hall", "10 New Gower Street, St. John's, NL", "10", "new gower", "john"),
    ("Whitehorse City Hall", "2121 2nd Avenue, Whitehorse, YT", "2121", "2nd", "whitehorse"),
    ("Yellowknife City Hall", "4807 52 Street, Yellowknife, NT", "4807", "52", "yellowknife"),
]
# label, text, hn, street words, city words. hn None = no exact door expected
VARIANTS = [
    ("abbreviated", "100 queen st w toronto", "100", "queen", "toronto"),
    ("no 'street'", "100 queen west toronto", "100", "queen", "toronto"),
    ("typo", "100 quen street west toronto", "100", "queen", "toronto"),
    ("unit prefix", "#200 - 100 Queen St W, Toronto", "100", "queen", "toronto"),
    ("no accents (FR)", "845 rue sherbrooke ouest montreal", "845", "sherbrooke", "montreal"),
    ("English street name", "845 Sherbrooke St W, Montreal", "845", "sherbrooke", "montreal"),
    ("FR lower-case", "1045 rue des parlementaires quebec", "1045", "parlementaires", "quebec"),
    ("postal code only", "M5H 2N2", None, None, "toronto"),
    ("place name", "Toronto City Hall", None, "city hall", "toronto"),
    ("number that does not exist", "99999 Queen Street West, Toronto", "99999", "queen", "toronto"),
    ("US address", "1600 Pennsylvania Avenue NW, Washington, DC", None, None, None),
]
TYPING = [0, 7, 11, 14, 19, 9]
STEPS = [3, 6, 9, 13, None]
RURAL = [("Trans-Canada Hwy near Wawa, ON", 47.9916, -84.7727), ("Banff townsite, AB", 51.1784, -115.5708), ("Prairie road near Regina, SK", 50.40, -104.40)]
US_POINT, QUEBEC_POINT = ("Buffalo, NY (outside Canada)", 42.8864, -78.8784), ("Montréal (Quebec)", 45.5017, -73.5673)


def place_ok(p, hn, street, city) -> bool:
    a = p.get("address") or {}
    return ((hn is None or a.get("house_number") == hn) and (street is None or has_words(a.get("street") or p.get("title"), street))
            and (city is None or has_words(f'{a.get("city") or ""} {p.get("title") or ""} {p.get("label") or ""}', city.split()[0]) or any(has_words(f'{a.get("city")} {p.get("label")}', c) for c in city.split())))


def sugg_ok(x, hn, street, city) -> bool:
    return (hn is None or hn in norm(x["title"]).split()) and (street is None or has_words(x["title"], street)) and (city is None or any(has_words(x["subtitle"] + " " + x["title"], c) for c in city.split()))


def photon_ok(pr, hn, street, city) -> bool:
    c = " ".join(str(pr.get(k) or "") for k in ("city", "town", "village", "district", "county", "name"))
    return ((hn is None or pr.get("housenumber") == hn) and (street is None or has_words(pr.get("street") or pr.get("name"), street))
            and (city is None or any(has_words(c, w) for w in city.split())))


def photon(q):
    r = timed("photon_search", lambda: requests.get("https://photon.komoot.io/api/", headers=UA, timeout=20, params={"q": q, "limit": 5, "lang": "en", "bbox": "-168,24,-52,72"}))
    r.raise_for_status()
    return r.json().get("features", [])


def photon_reverse(lat, lon):
    r = timed("photon_reverse", lambda: requests.get("https://photon.komoot.io/reverse", headers=UA, timeout=20, params={"lat": lat, "lon": lon, "lang": "en"}))
    r.raise_for_status()
    f = r.json().get("features", [])
    return f[0] if f else None


def nominatim(q):
    r = requests.get("https://nominatim.openstreetmap.org/search", headers=UA, timeout=20, params={"q": q, "format": "jsonv2", "addressdetails": 1, "limit": 1, "countrycodes": "ca"})
    r.raise_for_status()
    time.sleep(1.1)
    return r.json()


def api(tag, path, **kw):
    t = time.perf_counter()
    r = app.get(path, **kw)
    MS.setdefault(tag, []).append((time.perf_counter() - t) * 1000)
    return r


res = {"generated": time.strftime("%Y-%m-%d %H:%M"), "addresses": [], "variants": [], "typing": [], "reverse": [], "rural": [], "locate_rules": []}

# ---- 1. door-level addresses ----------------------------------------------------------------------------------
for name, q, hn, street, city in ADDR:
    row = {"name": name, "query": q, "hn": hn}
    g = api("apple_geocode", "/v1/places/geocode", params={"q": q, "limit": 3}).json().get("results", [])
    top = g[0] if g else None
    row["apple"] = None if not top else {"title": top["title"], "label": top.get("label"), "lat": top["lat"], "lon": top["lon"], "ok": place_ok(top, hn, street, city)}
    bias = {"lat": top["lat"] + 0.03, "lon": top["lon"]} if top else {}
    s0 = api("apple_suggest", "/v1/places/suggest", params={"q": q, "limit": 5}).json().get("results", [])
    s1 = api("apple_suggest", "/v1/places/suggest", params={"q": q, "limit": 5, **bias}).json().get("results", []) if bias else []
    row["suggest_nobias_top5"] = any(sugg_ok(x, hn, street, city) for x in s0)
    row["suggest_bias_top1"] = bool(s1) and sugg_ok(s1[0], hn, street, city)
    row["suggest_bias_top5"] = any(sugg_ok(x, hn, street, city) for x in s1)
    try:
        pf = photon(q)
        p0 = pf[0]["properties"] if pf else {}
        row["photon"] = {"ok": bool(pf) and photon_ok(p0, hn, street, city), "top5": any(photon_ok(f["properties"], hn, street, city) for f in pf),
                         "lat": pf[0]["geometry"]["coordinates"][1] if pf else None, "lon": pf[0]["geometry"]["coordinates"][0] if pf else None}
    except Exception as e:
        row["photon"] = {"error": type(e).__name__}
    try:
        nm = nominatim(q)
        a = (nm[0].get("address") if nm else {}) or {}
        row["osm"] = {"ok": bool(nm) and a.get("house_number") == hn and has_words(a.get("road"), street),
                      "lat": float(nm[0]["lat"]) if nm else None, "lon": float(nm[0]["lon"]) if nm else None}
    except Exception as e:
        row["osm"] = {"error": type(e).__name__}
    if row["apple"] and row["osm"].get("lat") is not None:
        row["apple_vs_osm_m"] = round(hav_m((row["apple"]["lat"], row["apple"]["lon"]), (row["osm"]["lat"], row["osm"]["lon"])))
    if row["photon"].get("lat") is not None and row["osm"].get("lat") is not None:
        row["photon_vs_osm_m"] = round(hav_m((row["photon"]["lat"], row["photon"]["lon"]), (row["osm"]["lat"], row["osm"]["lon"])))
    res["addresses"].append(row)
    time.sleep(0.3)

# ---- 2. messy input -------------------------------------------------------------------------------------------
for label, text, hn, street, city in VARIANTS:
    g = api("apple_geocode", "/v1/places/geocode", params={"q": text, "limit": 3}).json().get("results", [])
    s = api("apple_suggest", "/v1/places/suggest", params={"q": text, "limit": 5, "lat": 43.6532, "lon": -79.3832}).json().get("results", [])
    top = g[0] if g else None
    row = {"label": label, "text": text, "geocode": top and {"title": top["title"], "type": top["type"], "ok": place_ok(top, hn, street, city)},
           "suggest_first": s[0]["title"] + " | " + s[0]["subtitle"] if s else None, "suggest_top5_ok": any(sugg_ok(x, hn, street, city) for x in s) if (hn or street) else None}
    try:
        pf = photon(text)
        p = pf[0]["properties"] if pf else {}
        row["photon_first"] = f'{p.get("housenumber") or "-"} {p.get("street") or p.get("name") or ""}, {p.get("city") or p.get("state") or ""}, {p.get("country") or ""}' if pf else None
        row["photon_ok"] = bool(pf) and photon_ok(p, hn, street, city) and (p.get("countrycode") in (None, "CA") if label == "US address" else True)
    except Exception as e:
        row["photon_first"] = f"error {type(e).__name__}"
    res["variants"].append(row)
    time.sleep(0.3)

# ---- 3. typing, character by character -------------------------------------------------------------------------
pt = {r["name"]: (r["apple"]["lat"], r["apple"]["lon"]) for r in res["addresses"] if r["apple"]}
for i in TYPING:
    name, q, hn, street, city = ADDR[i]
    steps = []
    for n in STEPS:
        prefix = q if n is None else q[:n]
        ap0 = api("apple_suggest", "/v1/places/suggest", params={"q": prefix, "limit": 5}).json().get("results", [])
        ap1 = api("apple_suggest", "/v1/places/suggest", params={"q": prefix, "limit": 5, "lat": pt[name][0] + 0.03, "lon": pt[name][1]}).json().get("results", [])
        rk = lambda lst: next((k + 1 for k, x in enumerate(lst) if sugg_ok(x, hn, street, city)), None)
        try:
            pf = photon(prefix)
            prank = next((k + 1 for k, f in enumerate(pf) if photon_ok(f["properties"], hn, street, city)), None)
        except Exception:
            prank = "err"
        steps.append({"typed": prefix, "apple_nobias": rk(ap0), "apple_bias": rk(ap1), "photon": prank})
        time.sleep(0.25)
    res["typing"].append({"name": name, "steps": steps})

# ---- 4. "Locate Me": reverse geocoding on OpenStreetMap's point for each building -----------------------------------
for row in res["addresses"]:
    osm = row["osm"]
    if osm.get("lat") is None:
        continue
    name, hn = row["name"], row["hn"]
    street = next(a[3] for a in ADDR if a[0] == name)
    pt0 = (osm["lat"], osm["lon"])
    rec = {"name": name}
    r = api("apple_reverse", "/v1/places/reverse", params={"lat": pt0[0], "lon": pt0[1]})
    if r.status_code == 200:
        j = r.json()
        a = j.get("address") or {}
        rec["apple"] = {"label": j.get("label") or j.get("title"), "hn_ok": a.get("house_number") == hn, "street_ok": has_words(a.get("street"), street),
                        "has_street": bool(a.get("street")), "off_m": round(hav_m(pt0, (j["lat"], j["lon"])))}
    else:
        rec["apple"] = {"status": r.status_code}
    try:
        f = photon_reverse(*pt0)
        p = (f or {}).get("properties", {})
        c = (f or {}).get("geometry", {}).get("coordinates", [None, None])
        rec["photon"] = {"label": f'{p.get("housenumber") or ""} {p.get("street") or p.get("name") or ""}, {p.get("city") or p.get("town") or ""}'.strip(", "),
                         "hn_ok": p.get("housenumber") == hn, "street_ok": has_words(p.get("street"), street), "has_street": bool(p.get("street")),
                         "off_m": round(hav_m(pt0, (c[1], c[0]))) if c[0] is not None else None}
    except Exception as e:
        rec["photon"] = {"error": type(e).__name__}
    # a pin dropped 30 m north of the door (a dragged pin or GPS drift)
    pt1 = (pt0[0] + 30 / 111320, pt0[1])
    r2 = api("apple_reverse", "/v1/places/reverse", params={"lat": pt1[0], "lon": pt1[1]})
    rec["apple_drift_street_ok"] = r2.status_code == 200 and has_words((r2.json().get("address") or {}).get("street"), street)
    try:
        f2 = photon_reverse(*pt1)
        rec["photon_drift_street_ok"] = has_words(((f2 or {}).get("properties") or {}).get("street"), street)
    except Exception:
        rec["photon_drift_street_ok"] = None
    res["reverse"].append(rec)
    time.sleep(0.3)

for label, lat, lon in RURAL:
    r = api("apple_reverse", "/v1/places/reverse", params={"lat": lat, "lon": lon})
    f = None
    try:
        f = photon_reverse(lat, lon)
    except Exception:
        pass
    pp = (f or {}).get("properties", {})
    res["rural"].append({"label": label, "apple": (r.json().get("label") or r.json().get("title")) if r.status_code == 200 else f"HTTP {r.status_code}",
                         "photon": f'{pp.get("housenumber") or ""} {pp.get("street") or pp.get("name") or ""}, {pp.get("city") or pp.get("county") or ""}'.strip(", ") if f else "no result"})

# Locate Me rules as the node enforces them: a point outside Canada is refused before any provider call.
for label, lat, lon in (US_POINT, QUEBEC_POINT):
    r = api("apple_reverse", "/v1/places/reverse", params={"lat": lat, "lon": lon})
    res["locate_rules"].append({"label": label, "node_status": r.status_code, "node_answer": (r.json().get("label") if r.status_code == 200 else r.json()["error"]["code"]),
                                "note": "Komute's service_area.py also refuses Quebec pickups; the node itself does not" if "Quebec" in label else ""})

q = lambda v, p: round(sorted(v)[max(0, int(len(v) * p) - 1)])
res["latency_ms"] = {k: {"n": len(v), "median": round(statistics.median(v)), "p95": q(v, 0.95), "max": round(max(v))} for k, v in MS.items()}
OUT.parent.mkdir(exist_ok=True)
OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf8")
print("t3 done", {k: v["n"] for k, v in res["latency_ms"].items()})
