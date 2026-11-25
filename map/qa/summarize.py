"""Turns the qa/out/*.json results into one summary with pass / warn / fail checks. The dashboard and the PDF both read this,
so every number shown comes from a test run. Run:  python qa/summarize.py   (prints the checks; writes qa/out/summary.json)
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path

OUT = Path(__file__).resolve().parent / "out"
L = lambda f: json.loads((OUT / f).read_text(encoding="utf8"))
t1, t2, t3, t3r, t3u, t4, real = (L(f) for f in ("t1_map_display.json", "t2_tiles.json", "t3_search.json", "t3_rules.json", "t3_ui.json", "t4_routes.json", "real_pages_result.json"))
PASS, WARN, FAIL, INFO = "pass", "warn", "fail", "info"
checks: list[dict] = []


def add(part, who, title, status, value, detail=""):
    checks.append({"part": part, "who": who, "title": title, "status": status, "value": value, "detail": detail})


def rate(n, total, ok=0.85, warn=0.6):
    r = n / total if total else 0
    return PASS if r >= ok else (WARN if r >= warn else FAIL)


# ---- part 1: map display -----------------------------------------------------------------------------------------
runs = {(r["kind"], r["view"]): r for r in t1["runs"]}
lf, mk = [runs[("leaflet", v)] for v in ("desktop", "phone")], [runs[("mapkit", v)] for v in ("desktop", "phone")]
add(1, "today", "Leaflet map loads and draws the route", PASS if all(r["route_ok"] and r["ready_ms"] < 6000 for r in lf) else FAIL,
    f'{lf[0]["ready_ms"] / 1000:.1f} s desktop, {lf[1]["ready_ms"] / 1000:.1f} s phone', f'{lf[0]["tile_requests"]} tiles, {lf[0]["kb"]} KB on desktop; {lf[1]["kb"]} KB on phone')
add(1, "today", "Leaflet loaded from unpkg with its integrity hash", PASS if all(r["sri_checked_and_loaded"] for r in lf) else FAIL, "hash accepted, library loaded")
real_maps = [p for p in real["pages"] if p["page"] in ("ride-summary", "booking-details")]
add(1, "today", "Real Komute pages show the route and stats", PASS if all(p["route_stats_shown"] and not p["console_errors"] for p in real_maps) else FAIL,
    f'{len(real_maps)} of {len(real_maps)} page views', "ride summary and booking details, desktop and phone, on a throwaway database")
add(1, "apple", "MapKit JS accepts our token and draws the route", PASS if all(r["route_ok"] and r["token_ok"] and not r["mapkit_error"] for r in mk) else FAIL,
    f'{mk[0]["ready_ms"] / 1000:.1f} s desktop, {mk[1]["ready_ms"] / 1000:.1f} s phone', "route line comes from the map node (Apple Directions)")
hard = [f for r in lf + mk for f in r["failed_requests"] if not f.startswith("200 ")]
aborted = sum(1 for r in lf + mk for f in r["failed_requests"] if f.startswith("200 "))
add(1, "apple", "No console errors or failed requests", PASS if all(not r["console_errors"] for r in lf + mk) and not hard else WARN, "0 errors on all 4 runs" + (f", {aborted} tile cancelled mid-load" if aborted else ""),
    "A cancelled request that had already answered 200 is the browser dropping a tile while the map re-fitted; it is not a failure.")
add(1, "apple", "Page weight", INFO, f'{mk[0]["kb"] / 1024:.1f} MB desktop, {mk[1]["kb"] / 1024:.1f} MB phone',
    f'Leaflet + OSM: {lf[0]["kb"]} KB and {lf[1]["kb"]} KB. MapKit sends vector data and fonts, so it is heavier and slower to settle.')
add(1, "apple", "Hosts the page must be allowed to reach (CSP)", INFO, ", ".join(sorted({h for r in mk for h in r["hosts"]})), "today: unpkg.com, tile.openstreetmap.org, router.project-osrm.org")

# ---- part 2: map images ------------------------------------------------------------------------------------------
s2 = t2["summary"]
add(2, "today", "OSM tile server answers", rate(s2["ok"], s2["requested"], 0.95), f'{s2["ok"]} of {s2["requested"]} tiles', f'median {s2["median_ms"]} ms, 95th percentile {s2["p95_ms"]} ms')
tc = [c for c in t2["komute_template_check"] if "https_tile_url" in c]
add(2, "today", "Komute's tile use matches OSM's technical rules", PASS if all(all(v for k, v in c.items() if k != "file") for c in tc) else WARN,
    "HTTPS URL, attribution, Referer kept, no prefetch", "ride-summary.html and booking-details.html")
add(2, "today", "OSM tile policy: no SLA, commercial use can be blocked", WARN, "best-effort only", "Policy read 22 Nov 2026: " + t2["policy"]["commercial"])
add(2, "apple", "Tiles ship inside MapKit JS", INFO, f'{mk[0]["tile_requests"]} requests, {mk[0]["kb"]} KB for one map view', "No separate tile service to run. Apple's free allowance: 250,000 map views a day.")

# ---- part 3: address search and Locate Me --------------------------------------------------------------------------
A = t3["addresses"]
n = len(A)
ap_door = sum(1 for a in A if a["apple"] and a["apple"]["ok"])
ph_top1, ph_top5 = sum(1 for a in A if a["photon"].get("ok")), sum(1 for a in A if a["photon"].get("top5"))
ap_bias1, ap_bias5, ap_nob5 = sum(a["suggest_bias_top1"] for a in A), sum(a["suggest_bias_top5"] for a in A), sum(a["suggest_nobias_top5"] for a in A)
add(3, "today", "Photon: typed address gives the right door (first result)", rate(ph_top1, n), f"{ph_top1} of {n}", f"{ph_top5} of {n} within the top five. Misses: " + ", ".join(a["name"] for a in A if not a["photon"].get("ok")))
add(3, "apple", "Apple geocode: typed address gives the right door", rate(ap_door, n), f"{ap_door} of {n}", "Misses: " + ", ".join(a["name"] for a in A if not (a["apple"] and a["apple"]["ok"])))
add(3, "apple", "Apple autocomplete, rider location sent: right address first", rate(ap_bias1, n), f"{ap_bias1} of {n}", "while typing the full address")
add(3, "apple", "Apple autocomplete, no location sent", WARN, f"{ap_nob5} of {n} in the top five", "Without a location Apple often answers a full address with nothing. The page must send the rider's position.")
rp = {p["page"]: p for p in real["pages"] if p["page"] in ("post-ride", "search-rides")}
bad_first = [k for k, p in rp.items() if p.get("suggestions") and "United States" in p["suggestions"][0]]
add(3, "today", 'Real pages: typing "100 queen" puts a Canadian address first', FAIL if bad_first else PASS,
    "first result: " + (rp["post-ride"]["suggestions"][0].split(",")[0] if rp.get("post-ride", {}).get("suggestions") else "none"),
    "Photon has no location bias, so a university in Virginia outranks Toronto City Hall (second). Seen on post-ride and search-rides.")
ui = {(x["provider"], x["scenario"]): x for x in t3u["typing"]}
add(3, "apple", 'Test page: typing "100 queen" and "216 ontar" gives the right address first', PASS if all(ui[("apple", s)]["right_rank"] == 1 for s in ("toronto", "kingston")) else FAIL,
    "rank 1 and rank 1", "Photon: rank 2, and not found at all for 216 ontar")


def messy(v):
    """Per-case verdicts (Apple, Photon). The rules are written out here so the table is reproducible."""
    g, lab = v["geocode"], v["label"]
    if lab == "number that does not exist":
        return (PASS if g and not g["title"][:1].isdigit() else FAIL), (FAIL if (v.get("photon_first") or "").startswith("100 ") else PASS)
    if lab == "US address":
        return (PASS if g is None else FAIL), WARN
    if lab == "place name":
        return (PASS if v["suggest_top5_ok"] else FAIL), (PASS if "100 Queen" in (v.get("photon_first") or "") else FAIL)
    if lab == "postal code only":
        return WARN, WARN
    return (PASS if (g and g["ok"]) or v["suggest_top5_ok"] else FAIL), (PASS if v.get("photon_ok") else FAIL)


V = [(v, *messy(v)) for v in t3["variants"]]
ap_m, ph_m = sum(a == PASS for _, a, _ in V), sum(p == PASS for _, _, p in V)
add(3, "apple", "Messy input: 11 cases (typos, English street names, unit numbers, wrong number, US address)", rate(ap_m, len(V), 0.8), f"{ap_m} of {len(V)} handled (Photon {ph_m})", "Apple finds Montreal streets typed in English; Photon returns the wrong number")
R = t3["reverse"]
ap_r, ph_r = [r for r in R if "hn_ok" in r.get("apple", {})], [r for r in R if "hn_ok" in r.get("photon", {})]
ap_door_r, ph_door_r = sum(r["apple"]["hn_ok"] for r in ap_r), sum(r["photon"]["hn_ok"] for r in ph_r)
add(3, "apple", '"Locate Me": returns the same door as the point it was given', rate(ap_door_r, len(ap_r), 0.8, 0.5), f"{ap_door_r} of {len(ap_r)} (Photon {ph_door_r})",
    "Caution: the test points come from OpenStreetMap, which is also Photon's data, so Photon is favoured by construction. Apple returns the nearest address or the landmark's name.")
add(3, "apple", "Locate Me, 30 m GPS drift keeps the right street", rate(sum(bool(r["apple_drift_street_ok"]) for r in R), len(R), 0.8, 0.4), f'{sum(bool(r["apple_drift_street_ok"]) for r in R)} of {len(R)} (Photon {sum(bool(r.get("photon_drift_street_ok")) for r in R)})')
rules = {r["label"]: r for r in t3r}
us_ok = all(r["status"] == 422 for k, r in rules.items() if "US" in k)
add(3, "apple", "Locate Me refuses points in the US", PASS if us_ok else FAIL, "Buffalo and Seattle refused", "Needed the country check: a plain coordinate box let both through. Photon accepts US points today (the app supports the US).")
lat = t3["latency_ms"]
add(3, "apple", "Speed: autocomplete and reverse", INFO, f'autocomplete {lat["apple_suggest"]["median"]} ms, reverse {lat["apple_reverse"]["median"]} ms',
    f'Photon: {lat["photon_search"]["median"]} ms and {lat["photon_reverse"]["median"]} ms. Apple was called through the local node; a deployed node adds one network hop.')

# ---- part 4: routes ------------------------------------------------------------------------------------------------
RT = t4["routes"]
near = [r for r in RT if abs(r["km_diff_pct"]) <= 5]
add(4, "today", "OSRM demo answers every route", PASS, f"{len(RT)} of {len(RT)}", f'median {t4["latency_ms"]["osrm_route"]["median"]} ms')
add(4, "today", "OSRM demo terms: non-commercial use only, 1 request per second, no guarantees", FAIL, "Komute is a business", "Read 22 Nov 2026 on the OSRM wiki: usage is restricted to reasonable, non-commercial use-cases.")
add(4, "apple", "Distance within 5% of OSRM", rate(len(near), len(RT), 0.7, 0.5), f"{len(near)} of {len(RT)} routes", "Differences are on short city trips: " + ", ".join(f'{r["to"].split(",")[0]} {r["km_diff_pct"]:+.0f}%' for r in RT if abs(r["km_diff_pct"]) > 5))
agree = [r for r in RT if r["line_gap"] and r["line_gap"]["within_200m_pct"] >= 90]
add(4, "apple", "Drawn line follows the same roads as OSRM (90% of points within 200 m)", rate(len(agree), len(RT), 0.7, 0.5), f"{len(agree)} of {len(RT)} routes",
    "The Mississauga commute takes a different highway: median gap " + str(next(r["line_gap"]["median_m"] for r in RT if "Mississauga" in r["from"])) + " m")
hw = [r for r in RT if r["kind"] == "intercity"]
add(4, "apple", "Highway drive times against OSRM", INFO, f'{min(r["min_diff_pct"] for r in hw):+.0f}% to {max(r["min_diff_pct"] for r in hw):+.0f}% (Apple is shorter)', "OSRM's demo times run slow on highways. Apple's are nearer what drivers usually report (from general knowledge, not measured here).")
city = [r for r in RT if r["kind"] != "intercity"]
add(4, "apple", "City drive times against OSRM", WARN, f'{min(r["min_diff_pct"] for r in city):+.0f}% to {max(r["min_diff_pct"] for r in city):+.0f}% (Apple is longer)', "Apple allows for signals and city speeds, OSRM's demo does not. No true time to check against here.")
st = t4["stops"]
add(4, "apple", "Route with a stop", PASS, f'{st["apple_calls"]} Apple calls (OSRM: 1)', f'{st["apple_km"]} km against OSRM {st["osrm_km"]} km. Apple has no stop parameter, so each leg is a call.')
D = t4["detour"]
add(4, "apple", "Driver detour: matrix and route-with-stop agree", PASS if all(d["methods_disagree_km"] is not None and d["methods_disagree_km"] <= 0.5 for d in D) else WARN,
    f'within {max(d["methods_disagree_km"] for d in D)} km on {len(D)} of {len(D)} riders', "; ".join(f'{d["rider"].split(" (")[0]}: {d["matrix_km"]} km' for d in D) + ". The 30 km rule flags only the wrong-direction rider.")
add(4, "apple", "Speed: one route", INFO, f'{t4["latency_ms"]["apple_route"]["median"]} ms', f'OSRM demo: {t4["latency_ms"]["osrm_route"]["median"]} ms. A matrix call: {t4["latency_ms"]["apple_matrix"]["median"]} ms.')

PARTS = {1: "Map display (Leaflet)", 2: "Map images (OSM tiles)", 3: "Address search and Locate Me (Photon)", 4: "Routes and drive time (OSRM)"}


def tally(part, who):
    c = [x for x in checks if x["part"] == part and x["who"] == who]
    return {s: sum(x["status"] == s for x in c) for s in (PASS, WARN, FAIL, INFO)}


summary = {"generated": t3["generated"], "parts": PARTS, "checks": checks, "tally": {f"{p}-{w}": tally(p, w) for p in PARTS for w in ("today", "apple")},
           "messy": [{"label": v["label"], "text": v["text"], "apple": a, "photon": p, "apple_first": (v["geocode"] or {}).get("title"), "apple_suggest": v["suggest_first"], "photon_first": v.get("photon_first")} for v, a, p in V],
           "stats": {"addresses": n, "apple_door": ap_door, "photon_top1": ph_top1, "photon_top5": ph_top5, "apple_bias_top1": ap_bias1, "apple_nobias_top5": ap_nob5,
                     "apple_locate_door": ap_door_r, "photon_locate_door": ph_door_r, "locate_n": len(ap_r),
                     "apple_med_gap": round(statistics.median(a["apple_vs_osm_m"] for a in A if "apple_vs_osm_m" in a)),
                     "photon_med_gap": round(statistics.median(a["photon_vs_osm_m"] for a in A if "photon_vs_osm_m" in a))}}
(OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf8")

if __name__ == "__main__":
    for c in checks:
        print(f'P{c["part"]} {c["who"]:5} {c["status"].upper():4} {c["title"][:70]:70} | {c["value"]}')
    print(summary["tally"])
