"""Builds the dashboard page (one self-contained HTML file with the screenshots embedded) from qa/out/*.json.
Run:  python qa/build_dashboard.py   ->  qa/out/komute-map-dashboard.html
"""
from __future__ import annotations

import base64
import io
import json
from html import escape as e
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
S = json.loads((OUT / "summary.json").read_text(encoding="utf8"))
T3, T4, T2, REAL, T1 = (json.loads((OUT / f).read_text(encoding="utf8")) for f in ("t3_search.json", "t4_routes.json", "t2_tiles.json", "real_pages_result.json", "t1_map_display.json"))
RUN = {(r["kind"], r["view"]): r for r in T1["runs"]}
LF_S, MK_S = RUN[("leaflet", "desktop")]["ready_ms"] / 1000, RUN[("mapkit", "desktop")]["ready_ms"] / 1000
LF_KB, MK_MB = RUN[("leaflet", "desktop")]["kb"], RUN[("mapkit", "desktop")]["kb"] / 1024
CH, ST = S["checks"], S["stats"]
LABEL = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "info": "INFO"}


def img(name, width=720):
    """A screenshot as a small JPEG data URI."""
    im = Image.open(OUT / "shots" / name).convert("RGB")
    if im.width > width:
        im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=74, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def fig(name, caption, tag, width=720):
    return f'<figure><span class="tagline {tag}">{"Today" if tag == "today" else "Apple Maps"}</span><img src="{img(name, width)}" alt="{e(caption)}" loading="lazy"><figcaption>{e(caption)}</figcaption></figure>'


def chip(status):
    return f'<span class="chip {status}">{LABEL[status]}</span>'


def checks_table(part, who):
    rows = []
    for c in (c for c in CH if c["part"] == part and c["who"] == who):
        note = f'<div class="sub">{e(c["detail"])}</div>' if c["detail"] else ""
        rows.append(f'<tr><td>{chip(c["status"])}</td><td><b>{e(c["title"])}</b>{note}</td><td class="val">{e(c["value"])}</td></tr>')
    return f'<div class="tablewrap"><table><tbody>{"".join(rows)}</tbody></table></div>'


def tally_chips(part, who):
    t = S["tally"][f"{part}-{who}"]
    order = ("pass", "warn", "fail") if any(t[k] for k in ("pass", "warn", "fail")) else ("info",)
    return " ".join(f'<span class="chip {k}">{t[k]} {LABEL[k]}</span>' for k in order if t[k]) or '<span class="sub">no checks</span>'


def bars(rows):
    """rows: (label, today_value, apple_value, total). Two bars per row, same scale."""
    out = []
    for label, a, b, total in rows:
        out.append(f'<div class="brow"><div class="blabel">{e(label)}</div><div class="bpair">'
                   f'<div class="bar"><span class="bt">Photon today</span><i class="today" style="width:{a / total * 100:.0f}%"></i><b>{a}/{total}</b></div>'
                   f'<div class="bar"><span class="bt">Apple Maps</span><i class="apple" style="width:{b / total * 100:.0f}%"></i><b>{b}/{total}</b></div></div></div>')
    return "".join(out)


M = {m["label"]: m for m in S["messy"]}
messy_rows = "".join(
    f'<tr><td><b>{e(m["label"])}</b><div class="sub mono">{e(m["text"])}</div></td><td>{chip(m["apple"])}<div class="sub">{e(m["apple_first"] or "no result")}</div></td>'
    f'<td>{chip(m["photon"])}<div class="sub">{e(m["photon_first"] or "")}</div></td></tr>' for m in S["messy"])

addr_rows = "".join(
    f'<tr><td><b>{e(a["name"])}</b><div class="sub">{e(a["query"])}</div></td><td>{chip("pass" if a["apple"] and a["apple"]["ok"] else "fail")}<div class="sub">{e(a["apple"]["title"]) if a["apple"] else "no result"}</div></td>'
    f'<td>{chip("pass" if a["photon"].get("ok") else "fail")}</td><td>{chip("pass" if a["osm"].get("ok") else "fail")}</td></tr>' for a in T3["addresses"])

route_rows = "".join(
    f'<tr><td><b>{e(r["from"])}</b> to {e(r["to"])}<div class="sub">{e(r["kind"])}, {r["straight_km"]} km straight</div></td>'
    f'<td class="val">{r["apple_km"]} km<br>{r["apple_min"]} min</td><td class="val">{r["osrm_km"]} km<br>{r["osrm_min"]} min</td>'
    f'<td class="val">{chip("pass" if abs(r["km_diff_pct"]) <= 5 else "warn")} {r["km_diff_pct"]:+.0f}%<div class="sub">time {r["min_diff_pct"]:+.0f}%</div></td>'
    f'<td class="val">{r["line_gap"]["within_200m_pct"]}% within 200 m<div class="sub">median gap {r["line_gap"]["median_m"]} m</div></td></tr>' for r in T4["routes"])
detour_rows = "".join(
    f'<tr><td><b>{e(d["rider"])}</b><div class="sub">{e(d["why"])}</div></td><td class="val">{d["matrix_km"]} km / {d["matrix_min"]} min</td><td class="val">{d["route_km"]} km / {d["route_min"]} min</td>'
    f'<td class="val">{d["osrm_km"]} km / {d["osrm_min"]} min</td><td>{chip("fail" if d["over_30km_apple"] else "pass")}<div class="sub">{"over" if d["over_30km_apple"] else "within"} 30 km</div></td></tr>' for d in T4["detour"])
tile_rows = "".join(f'<tr><td class="mono">{e(c)}</td><td class="val">{n} of 6</td></tr>' for c, n in
                    ((c, sum(1 for t in T2["tiles"] if t["city"] == c and t.get("status") == 200)) for c in dict.fromkeys(t["city"] for t in T2["tiles"])))
lat = T3["latency_ms"]
lat4 = T4["latency_ms"]
lat_rows = (f'<tr><td>Address autocomplete</td><td class="val">{lat["photon_search"]["median"]} ms</td><td class="val">{lat["apple_suggest"]["median"]} ms</td></tr>'
            f'<tr><td>Locate Me (reverse)</td><td class="val">{lat["photon_reverse"]["median"]} ms</td><td class="val">{lat["apple_reverse"]["median"]} ms</td></tr>'
            f'<tr><td>One route</td><td class="val">{lat4["osrm_route"]["median"]} ms (OSRM)</td><td class="val">{lat4["apple_route"]["median"]} ms</td></tr>'
            f'<tr><td>Map ready on screen</td><td class="val">{LF_S:.1f} s (Leaflet)</td><td class="val">{MK_S:.1f} s (MapKit)</td></tr>')

HTML = f"""<title>Komute Map Stack Dashboard</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Sans+Condensed:wght@600&display=swap">
<style>
/* layout: one long dashboard. Verdict, a scoreboard of the four parts, then one section per part: checks first, pictures after. */
:root {{
  --bg:#f4f6f7; --surface:#ffffff; --ink:#14212b; --muted:#55656f; --line:#d9e0e4; --accent:#0b6e7a; --today:#7a8a94; --apple:#0b6e7a;
  --ok:#17703b; --ok-bg:#e0f3e7; --warn:#8a5a00; --warn-bg:#fbefd0; --bad:#a3231f; --bad-bg:#fbe2e0; --info:#37566b; --info-bg:#e4edf3; --bar-bg:#e3eaed;
  --sans:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif; --head:"IBM Plex Sans Condensed","IBM Plex Sans",system-ui,sans-serif; --mono:"IBM Plex Mono",ui-monospace,Consolas,monospace;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg:#0f1a21; --surface:#16252e; --ink:#e6eef2; --muted:#93a7b2; --line:#2a3d48; --accent:#4fc3d0; --today:#8196a2; --apple:#4fc3d0;
  --ok:#6fd596; --ok-bg:#153a26; --warn:#f0c36a; --warn-bg:#3d3010; --bad:#ff9a94; --bad-bg:#431b19; --info:#9fc3da; --info-bg:#1e3442; --bar-bg:#26383f; color-scheme:dark; }} }}
:root[data-theme="dark"] {{
  --bg:#0f1a21; --surface:#16252e; --ink:#e6eef2; --muted:#93a7b2; --line:#2a3d48; --accent:#4fc3d0; --today:#8196a2; --apple:#4fc3d0;
  --ok:#6fd596; --ok-bg:#153a26; --warn:#f0c36a; --warn-bg:#3d3010; --bad:#ff9a94; --bad-bg:#431b19; --info:#9fc3da; --info-bg:#1e3442; --bar-bg:#26383f; color-scheme:dark; }}
* {{ box-sizing:border-box; }}
body {{ background:var(--bg); color:var(--ink); font:15px/1.55 var(--sans); padding-block:28px 72px;
  padding-left:max(16px, env(safe-area-inset-left,0px)); padding-right:max(16px, env(safe-area-inset-right,0px)); }}
main {{ max-width:1080px; margin:0 auto; display:grid; gap:34px; }}
h1,h2,h3 {{ font-family:var(--head); font-weight:600; letter-spacing:.01em; text-wrap:balance; margin:0; }}
h1 {{ font-size:clamp(1.9rem,4.5vw,2.7rem); line-height:1.1; }} h2 {{ font-size:1.4rem; }} h3 {{ font-size:.82rem; text-transform:uppercase; letter-spacing:.09em; color:var(--muted); font-family:var(--mono); font-weight:500; }}
p {{ margin:0; max-width:70ch; }} .lead {{ color:var(--muted); font-size:1.02rem; }} .sub {{ color:var(--muted); font-size:.84rem; }} .mono {{ font-family:var(--mono); font-size:.86em; }}
.kicker {{ font:500 .78rem var(--mono); letter-spacing:.08em; text-transform:uppercase; color:var(--accent); }}
section {{ display:grid; gap:14px; }}
.verdict {{ background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:20px 22px; display:grid; gap:12px; }}
.verdict ul {{ margin:0; padding:0; list-style:none; display:grid; gap:9px; }} .verdict li {{ display:grid; grid-template-columns:auto 1fr; gap:10px; align-items:baseline; }}
.score {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(235px,1fr)); gap:12px; }}
.card {{ background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:14px 16px; display:grid; gap:8px; min-width:0; align-content:start; }}
.card .num {{ font:600 .74rem var(--mono); color:var(--muted); letter-spacing:.06em; }} .card b.t {{ font:600 1.08rem var(--head); }}
.who {{ display:grid; gap:2px; padding-top:6px; border-top:1px solid var(--line); }} .who .w {{ font:500 .72rem var(--mono); text-transform:uppercase; letter-spacing:.08em; color:var(--muted); }}
.chip {{ display:inline-block; font:500 .72rem var(--mono); padding:2px 8px; border-radius:99px; white-space:nowrap; margin-right:3px; }}
.chip.pass {{ background:var(--ok-bg); color:var(--ok); }} .chip.warn {{ background:var(--warn-bg); color:var(--warn); }} .chip.fail {{ background:var(--bad-bg); color:var(--bad); }} .chip.info {{ background:var(--info-bg); color:var(--info); }}
.tablewrap {{ overflow-x:auto; background:var(--surface); border:1px solid var(--line); border-radius:10px; }}
table {{ width:100%; border-collapse:collapse; min-width:560px; }} th,td {{ text-align:left; vertical-align:top; padding:9px 12px; border-bottom:1px solid var(--line); }}
th {{ font:500 .72rem var(--mono); letter-spacing:.06em; text-transform:uppercase; color:var(--muted); background:var(--bg); }} tr:last-child td {{ border-bottom:0; }}
td.val {{ font-variant-numeric:tabular-nums; white-space:nowrap; }}
.who-h {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }} .who-h h3 {{ margin:0; }}
.figs {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr)); gap:14px; }}
figure {{ margin:0; background:var(--surface); border:1px solid var(--line); border-radius:10px; overflow:hidden; display:grid; min-width:0; align-content:start; }}
figure img {{ width:100%; height:auto; display:block; background:var(--bar-bg); }} figcaption {{ padding:8px 12px 10px; font-size:.84rem; color:var(--muted); }}
.tagline {{ font:500 .7rem var(--mono); letter-spacing:.08em; text-transform:uppercase; padding:5px 12px; }}
.tagline.today {{ background:var(--bar-bg); color:var(--muted); }} .tagline.apple {{ background:var(--info-bg); color:var(--accent); }}
.brow {{ display:grid; grid-template-columns:minmax(150px,1fr) 2fr; gap:12px; align-items:center; padding:8px 0; border-bottom:1px solid var(--line); }} .brow:last-child {{ border-bottom:0; }}
.blabel {{ font-size:.9rem; }} .bpair {{ display:grid; gap:5px; }}
.bar {{ position:relative; display:flex; align-items:center; gap:8px; height:22px; background:var(--bar-bg); border-radius:5px; overflow:hidden; }}
.bar i {{ position:absolute; inset:0 auto 0 0; border-radius:5px; opacity:.85; }} .bar i.today {{ background:var(--today); }} .bar i.apple {{ background:var(--apple); }}
.bar .bt,.bar b {{ position:relative; font:500 .72rem var(--mono); padding:0 8px; color:var(--ink); }} .bar b {{ margin-left:auto; }}
.chart {{ background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:6px 16px; }}
.callout {{ border-left:3px solid var(--accent); padding:4px 0 4px 14px; max-width:74ch; display:grid; gap:4px; }} .callout.warn {{ border-color:var(--warn); }} .callout.bad {{ border-color:var(--bad); }}
.two {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:14px; }} .two > * {{ min-width:0; }}
ol.steps {{ margin:0; padding:0; list-style:none; display:grid; gap:10px; }} ol.steps li {{ background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:12px 14px; display:grid; gap:3px; }}
ol.steps b {{ font-family:var(--head); font-size:1.02rem; }}
details {{ background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:10px 14px; }} summary {{ cursor:pointer; font-weight:600; }}
footer {{ color:var(--muted); font-size:.84rem; max-width:78ch; }}
@media (max-width:560px) {{ .brow {{ grid-template-columns:1fr; gap:4px; }} body {{ font-size:14.5px; }} }}
</style>

<main>
<header style="display:grid;gap:10px">
  <span class="kicker">Komute · map stack test · 4 parts · run of {e(S["generated"])}</span>
  <h1>Komute Map Stack Dashboard</h1>
  <p class="lead">The four map parts Komute uses today (Leaflet, the OpenStreetMap tile server, Photon, OSRM) tested on real Canadian addresses and routes, next to the same jobs done by Apple Maps through the new map node. Every number below comes from a test script in <span class="mono">map/qa</span>. The pictures are from Komute's real pages and from standalone Apple test pages.</p>
</header>

<div class="verdict">
  <h2>Can Apple Maps replace Photon completely? Yes for address search. "Locate Me" works but is less exact at the door.</h2>
  <ul>
    <li>{chip("pass")}<span><b>Search is better.</b> With the rider's location sent, Apple puts the right address first for {ST["apple_bias_top1"]} of {ST["addresses"]} typed addresses. Photon got {ST["photon_top1"]}, and on Komute's real post-ride page it ranks a Virginia university above Toronto City Hall for "100 queen".</span></li>
    <li>{chip("warn")}<span><b>Locate Me is a step down on the door number.</b> Apple returned the same door as the point for {ST["apple_locate_door"]} of {ST["locate_n"]}, Photon for {ST["photon_locate_door"]}. My test points come from OpenStreetMap, which is Photon's own data, so this favours Photon. A fair field test needs a phone at real addresses.</span></li>
    <li>{chip("fail")}<span><b>Three conditions before anything is removed:</b> read Apple's license (it is quoted as limiting stored map data), decide what happens to US riders (Photon accepts them, the node does not), and send the rider's location with every autocomplete call.</span></li>
    <li>{chip("fail")}<span><b>Separately, OSRM should go regardless.</b> Its demo server is for non-commercial use only, at most 1 request per second, with no guarantees.</span></li>
  </ul>
</div>

<section>
  <h2>Scoreboard</h2>
  <div class="score">
""" + "".join(
    f'<div class="card"><span class="num">PART {p}</span><b class="t">{e(name)}</b><div class="who"><span class="w">Today</span><div>{tally_chips(p, "today")}</div></div>'
    f'<div class="who"><span class="w">Apple Maps</span><div>{tally_chips(p, "apple")}</div></div></div>' for p, name in S["parts"].items()) + f"""
  </div>
</section>

<section>
  <h2>1. Map display: Leaflet 1.9.4 and MapKit JS</h2>
  <p class="lead">The route map on the ride summary and booking pages. Same ride in every picture: Toronto City Hall, a stop in Kingston, Parliament Hill in Ottawa.</p>
  <div class="who-h"><h3>Today</h3>{tally_chips(1, "today")}</div>{checks_table(1, "today")}
  <div class="who-h"><h3>Apple Maps</h3>{tally_chips(1, "apple")}</div>{checks_table(1, "apple")}
  <div class="figs">
    {fig("real-ride-summary-desktop.png", "Komute's real ride summary page, desktop (Leaflet, OSM tiles, OSRM line)", "today")}
    {fig("t1-mapkit-desktop.png", "The same ride on MapKit JS with the line from the map node, desktop", "apple")}
    {fig("real-booking-details-phone.png", "Komute's real booking page at phone width", "today", 420)}
    {fig("t1-mapkit-phone.png", "MapKit JS at phone width", "apple", 420)}
  </div>
  <div class="callout"><b>What this shows</b><p>Both work with no errors. Leaflet is faster and far lighter ({LF_S:.1f} s and {LF_KB} KB against {MK_S:.1f} s and {MK_MB:.1f} MB on desktop). MapKit's map has labels, parks and road shields, which OSM's raster tiles do not show at this zoom.</p></div>
</section>

<section>
  <h2>2. Map images: the OpenStreetMap tile server</h2>
  <p class="lead">The public server at tile.openstreetmap.org works well today. The risk is its policy, not its speed.</p>
  <div class="who-h"><h3>Today</h3>{tally_chips(2, "today")}</div>{checks_table(2, "today")}
  <div class="who-h"><h3>Apple Maps</h3>{tally_chips(2, "apple")}</div>{checks_table(2, "apple")}
  <div class="two">
    <div class="tablewrap"><table style="min-width:0"><thead><tr><th>City sampled</th><th>Tiles answered</th></tr></thead><tbody>{tile_rows}</tbody></table></div>
    {fig("real-booking-details-desktop.png", "Komute's real booking page: the OSM tiles behind the Leaflet map", "today")}
  </div>
  <div class="callout warn"><b>What this shows</b><p>24 of 24 tiles came back in about 0.1 s. The policy says there is no service guarantee, and that commercial services can be blocked without notice. Komute would have no fallback if that happened. Apple's tiles come with MapKit JS under Apple's quota (250,000 map views a day).</p></div>
</section>

<section>
  <h2>3. Address search and "Locate Me": Photon and Apple Maps</h2>
  <p class="lead">Post ride, edit ride and search all use this. {ST["addresses"]} public buildings in every province and territory except Nunavut, plus messy input, typing, and phone-GPS cases.</p>
  <div class="chart">{bars([("Typed address: right door first", ST["photon_top1"], ST["apple_door"], ST["addresses"]), ("Autocomplete: right address first", ST["photon_top1"], ST["apple_bias_top1"], ST["addresses"]), ("Messy input handled (11 cases)", sum(m["photon"] == "pass" for m in S["messy"]), sum(m["apple"] == "pass" for m in S["messy"]), 11), ("Locate Me: same door number", ST["photon_locate_door"], ST["apple_locate_door"], ST["locate_n"])])}</div>
  <p class="sub">Apple autocomplete figures assume the rider's location is sent. The Locate Me bars favour Photon because the test points are OpenStreetMap's own.</p>
  <div class="who-h"><h3>Today (Photon)</h3>{tally_chips(3, "today")}</div>{checks_table(3, "today")}
  <div class="who-h"><h3>Apple Maps</h3>{tally_chips(3, "apple")}</div>{checks_table(3, "apple")}
  <div class="figs">
    {fig("real-post-ride-type.png", "Komute's real post-ride form: typing 100 queen puts a Virginia university first", "today")}
    {fig("t3-type-toronto-apple.png", "Apple test page: the same typing, right address first", "apple", 540)}
    {fig("real-post-ride-locate.png", "Locate Me on the real post-ride page at downtown Toronto (fake GPS)", "today")}
    {fig("t3-locate-toronto-apple.png", "Apple: same GPS point. It returns the landmark's name, not the door", "apple", 540)}
    {fig("t3-locate-buffalo-photon.png", "Buffalo, NY: Photon accepts a US point (Komute supports the US today)", "today", 540)}
    {fig("t3-locate-buffalo-apple.png", "Buffalo, NY: the Apple node refuses (Canada only)", "apple", 540)}
  </div>
  <details><summary>Messy input, case by case</summary><div class="tablewrap" style="margin-top:10px"><table><thead><tr><th>Case</th><th>Apple Maps</th><th>Photon</th></tr></thead><tbody>{messy_rows}</tbody></table></div></details>
  <details><summary>All {ST["addresses"]} typed addresses</summary><div class="tablewrap" style="margin-top:10px"><table><thead><tr><th>Address typed</th><th>Apple Maps</th><th>Photon</th><th>OSM</th></tr></thead><tbody>{addr_rows}</tbody></table></div></details>
</section>

<section>
  <h2>4. Routes, distance and drive time: OSRM and Apple Directions</h2>
  <p class="lead">Used for the drive time stored on a ride and the line drawn on the map. Seven routes, one with a stop, and three driver-detour riders.</p>
  <div class="who-h"><h3>Today (OSRM demo server)</h3>{tally_chips(4, "today")}</div>{checks_table(4, "today")}
  <div class="who-h"><h3>Apple Maps</h3>{tally_chips(4, "apple")}</div>{checks_table(4, "apple")}
  <div class="tablewrap"><table><thead><tr><th>Route</th><th>Apple</th><th>OSRM</th><th>Distance gap</th><th>Line agreement</th></tr></thead><tbody>{route_rows}</tbody></table></div>
  <div class="tablewrap"><table><thead><tr><th>Driver detour for a rider pickup</th><th>Apple matrix</th><th>Apple route + stop</th><th>OSRM</th><th>30 km rule</th></tr></thead><tbody>{detour_rows}</tbody></table></div>
  <div class="figs">
    {fig("t1-leaflet-desktop.png", "Today: line from OSRM, 459.9 km", "today")}
    {fig("t1-mapkit-desktop.png", "Apple: line from Apple Directions, 462.8 km", "apple")}
  </div>
  <div class="callout warn"><b>What this shows</b><p>Apple agrees with OSRM on distance within 5% for most routes and draws almost the same roads (most points within 200 m). Highway times come out shorter than OSRM's (see the table). City times are much longer, which may include signals and speed limits, and there is no true time here to check which is right. Replace OSRM either way: its demo terms rule out commercial use.</p></div>
</section>

<section>
  <h2>Speed</h2>
  <div class="tablewrap"><table><thead><tr><th>Step</th><th>Today</th><th>Apple Maps</th></tr></thead><tbody>{lat_rows}</tbody></table></div>
  <p class="sub">Medians from this test machine, one call at a time. Apple was called through a node on the same machine, so a deployed node adds one network hop.</p>
</section>

<section>
  <h2>Removing Photon: the plan, in order</h2>
  <ol class="steps">
    <li><b>0. Settle two questions first</b><span class="sub">Does Apple's license (Schedule 6) let Komute store ride coordinates and show results on a non-Apple map? Does Komute stay US and Canada, or Canada only?</span></li>
    <li><b>1. Komute's own endpoints</b><span class="sub">Add suggest, details and reverse views in Django that call the map node with the shared key, with rate limiting and the Quebec service-area check. The browser never sees an Apple key.</span></li>
    <li><b>2. Swap the two Photon uses</b><span class="sub">static/js/place-autocomplete.js (autocomplete on three pages) and the two Locate Me blocks in post-ride.html and search-rides.html. Send the rider's location with every autocomplete call.</span></li>
    <li><b>3. Tidy the edges</b><span class="sub">Remove photon.komoot.io from the content security policy, rewrite the privacy policy lines that name Komoot, update the regression tests that assert those hosts.</span></li>
    <li><b>4. Ship and watch</b><span class="sub">Run the node as a service, set MAP_API_URL and MAP_API_KEY, alert at 80% of Apple's 25,000 calls a day, and ask Apple for more if growth needs it.</span></li>
  </ol>
  <div class="callout bad"><b>Quota to plan for</b><p>Apple's allowance is 25,000 service calls a day per team, shared by search and the browser map. Typing one address costs roughly 3 to 6 calls (my estimate from the 300 ms typing delay, not measured), so about 2,000 searches a day. The node caches repeat queries for 5 minutes.</p></div>
</section>

<footer>
  <p><b>Limits of this test.</b> One computer, one Sunday afternoon, no weekday traffic. "Gap to OSM" and the Locate Me door check use OpenStreetMap points as the reference, which favours Photon. My expected house numbers come from my own knowledge of the buildings. Not tested: units and apartments, rural Quebec, entrance-level access, a real phone's GPS drift, Apple's and OSM's terms beyond what was read, or any change to Komute's real pages. "Today" pictures are Komute's real pages on a throwaway database. "Apple" pictures are standalone test pages calling the map node. Scripts: map/qa/t1 to t4, real_pages.mjs, seed_real_pages.py.</p>
</footer>
</main>
"""
out = OUT / "komute-map-dashboard.html"
out.write_text(HTML, encoding="utf8")
print(f"dashboard written: {len(HTML) / 1024:.0f} KB")
