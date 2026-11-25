"""Builds the full results PDF (checks, tables and screenshots) from qa/out/*.json. Uses ReportLab, already installed.
Run:  python qa/build_pdf.py   ->  docs/Map_Stack_Test_Report.pdf   (docs/ is git-ignored, like Verification_Node_Change_Report.pdf)
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
DEST = HERE.parents[1] / "docs" / "Map_Stack_Test_Report.pdf"
J = lambda f: json.loads((OUT / f).read_text(encoding="utf8"))
S, T1, T2, T3, T4 = J("summary.json"), J("t1_map_display.json"), J("t2_tiles.json"), J("t3_search.json"), J("t4_routes.json")
CH, ST = S["checks"], S["stats"]

INK, MUTED, LINE = colors.HexColor("#14212b"), colors.HexColor("#55656f"), colors.HexColor("#d9e0e4")
COL = {"pass": colors.HexColor("#17703b"), "warn": colors.HexColor("#8a5a00"), "fail": colors.HexColor("#a3231f"), "info": colors.HexColor("#37566b")}
BG = {"pass": colors.HexColor("#e0f3e7"), "warn": colors.HexColor("#fbefd0"), "fail": colors.HexColor("#fbe2e0"), "info": colors.HexColor("#e4edf3")}
base = getSampleStyleSheet()
st = {
    "h1": ParagraphStyle("h1", parent=base["Title"], fontName="Helvetica-Bold", fontSize=24, leading=28, alignment=0, textColor=INK, spaceAfter=4),
    "h2": ParagraphStyle("h2", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=15, leading=19, textColor=INK, spaceBefore=14, spaceAfter=6),
    "h3": ParagraphStyle("h3", parent=base["Heading3"], fontName="Helvetica-Bold", fontSize=9.5, leading=12, textColor=MUTED, spaceBefore=8, spaceAfter=3),
    "p": ParagraphStyle("p", parent=base["BodyText"], fontName="Helvetica", fontSize=9.6, leading=13.4, textColor=INK, spaceAfter=5),
    "sub": ParagraphStyle("sub", parent=base["BodyText"], fontName="Helvetica", fontSize=8, leading=10.5, textColor=MUTED),
    "cell": ParagraphStyle("cell", parent=base["BodyText"], fontName="Helvetica", fontSize=8.4, leading=10.8, textColor=INK),
    "cellb": ParagraphStyle("cellb", parent=base["BodyText"], fontName="Helvetica-Bold", fontSize=8.4, leading=10.8, textColor=INK),
    "cap": ParagraphStyle("cap", parent=base["BodyText"], fontName="Helvetica", fontSize=7.6, leading=9.6, textColor=MUTED),
}
SAFE = {"→": "to", "≤": "<=", "≥": ">=", "≈": "~", "–": "-", "—": "-", "’": "'", "“": '"', "”": '"', "×": "x", "…": "..."}


def t(s) -> str:
    s = str(s)
    for k, v in SAFE.items():
        s = s.replace(k, v)
    return escape(s.encode("cp1252", "replace").decode("cp1252"))


P = lambda text, style="p": Paragraph(t(text), st[style])
STATUS_TXT = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "info": "INFO"}


def status_cell(s):
    return Paragraph(f'<font color="{COL[s].hexval()}"><b>{STATUS_TXT[s]}</b></font>', st["cell"])


def table(rows, widths, header=True, zebra=False, extra=()):
    tb = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE), ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
             ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4)]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f4"))]
    tb.setStyle(TableStyle(style + list(extra)))
    return tb


def checks_block(part, who, title):
    rows = [c for c in CH if c["part"] == part and c["who"] == who]
    data = [[status_cell(c["status"]), [Paragraph(f"<b>{t(c['title'])}</b>", st["cell"])] + ([Paragraph(t(c["detail"]), st["sub"])] if c["detail"] else []), Paragraph(t(c["value"]), st["cell"])] for c in rows]
    return [P(title, "h3"), table(data, [0.5 * inch, 4.75 * inch, 1.85 * inch], header=False)]


def shot(name, width_in, quality=78):
    im = PILImage.open(OUT / "shots" / name).convert("RGB")
    px = int(width_in * 170)
    if im.width > px:
        im = im.resize((px, round(im.height * px / im.width)), PILImage.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    buf.seek(0)
    return Image(buf, width=width_in * inch, height=width_in * inch * im.height / im.width)


def pair(a, ca, b, cb, w=3.4):
    """Two screenshots side by side with captions. a = today, b = Apple."""
    tb = Table([[shot(a, w), shot(b, w)], [Paragraph("<b>TODAY</b> - " + t(ca), st["cap"]), Paragraph("<b>APPLE MAPS</b> - " + t(cb), st["cap"])]], colWidths=[3.55 * inch, 3.55 * inch])
    tb.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 6)]))
    return KeepTogether([tb, Spacer(1, 8)])


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(0.7 * inch, 0.45 * inch, "Komute map stack test report - run of " + S["generated"])
    canvas.drawRightString(letter[0] - 0.7 * inch, 0.45 * inch, f"Page {doc.page}")
    canvas.restoreState()


story = []
story += [P("Komute map stack test report", "h1"), P(f"Leaflet, OpenStreetMap tiles, Photon and OSRM, tested live against Apple Maps. Run of {S['generated']}. Every figure comes from a script in map/qa.", "sub"), Spacer(1, 8)]
story += [P("Verdict", "h2"),
          P(f"Can Apple Maps replace Photon completely? Yes for address search. With the rider's location sent, Apple put the right address first for {ST['apple_bias_top1']} of {ST['addresses']} typed addresses against Photon's {ST['photon_top1']}. On Komute's real post-ride page, typing '100 queen' puts Hampton University in Virginia first and Toronto City Hall second."),
          P(f"'Locate Me' works but is less exact on the door number: Apple returned the same door as the test point for {ST['apple_locate_door']} of {ST['locate_n']}, Photon for {ST['photon_locate_door']}. The test points come from OpenStreetMap, which is Photon's own data, so this comparison favours Photon. A fair test needs a phone standing at real addresses."),
          P("Three conditions before anything is removed: read Apple's license (it is quoted as limiting stored map data and display off an Apple map), decide what happens to US riders (Photon accepts them, the node is Canada only), and send the rider's location with every autocomplete call."),
          P("Separately, OSRM should be replaced whatever is decided about Photon. Its demo server is for non-commercial use only, at most 1 request per second, with no guarantees."), Spacer(1, 6)]
rows = [[P("Part", "cellb"), P("Today", "cellb"), P("Apple Maps", "cellb")]]
for p, name in S["parts"].items():
    def ty(w):
        tl = S["tally"][f"{p}-{w}"]
        order = ("pass", "warn", "fail") if any(tl[k] for k in ("pass", "warn", "fail")) else ("info",)
        return Paragraph(" ".join(f'<font color="{COL[k].hexval()}"><b>{tl[k]} {STATUS_TXT[k]}</b></font>' for k in order if tl[k]) or "-", st["cell"])
    rows.append([P(f"{p}. {name}", "cellb"), ty("today"), ty("apple")])
story += [P("Scoreboard", "h2"), table(rows, [3.3 * inch, 1.9 * inch, 1.9 * inch])]

# ---- part 1 --------------------------------------------------------------------------------------------------
story += [PageBreak(), P("1. Map display: Leaflet 1.9.4 and MapKit JS", "h2"),
          P("The route map on the ride summary and booking pages. Same ride in every picture: Toronto City Hall, a stop in Kingston, Parliament Hill in Ottawa. Today's pictures are Komute's real pages running on a throwaway database.")]
story += checks_block(1, "today", "TODAY") + checks_block(1, "apple", "APPLE MAPS")
story += [Spacer(1, 8), pair("real-ride-summary-desktop.png", "real ride summary page, desktop (Leaflet, OSM tiles, OSRM line)", "t1-mapkit-desktop.png", "same ride on MapKit JS, line from the map node"),
          pair("real-booking-details-phone.png", "real booking page at phone width", "t1-mapkit-phone.png", "MapKit JS at phone width", 2.2),
          P("Both work with no console errors. Leaflet is faster and much lighter. MapKit's map has labels, parks and road shields that OSM's raster tiles do not show at this zoom.")]

# ---- part 2 --------------------------------------------------------------------------------------------------
story += [PageBreak(), P("2. Map images: the OpenStreetMap tile server", "h2"),
          P("tile.openstreetmap.org works well today. The risk is the policy, not the speed. The policy page was read on the day of the run: " + T2["policy"]["url"])]
story += checks_block(2, "today", "TODAY") + checks_block(2, "apple", "APPLE MAPS")
pol = [[P("Rule from the policy", "cellb"), P("Detail", "cellb")],
       [P("Service level"), P(T2["policy"]["sla"])], [P("Commercial use"), P(T2["policy"]["commercial"])],
       [P("Must"), P("; ".join(T2["policy"]["must"]))], [P("Must not"), P("; ".join(T2["policy"]["must_not"]))]]
story += [Spacer(1, 6), table([[Paragraph(t(c.text if hasattr(c, "text") else c), st["cell"]) if not hasattr(c, "text") else c for c in r] for r in pol], [1.5 * inch, 5.6 * inch]), Spacer(1, 8),
          pair("real-booking-details-desktop.png", "Komute's real booking page: the OSM tiles behind the Leaflet map", "t1-mapkit-desktop.png", "Apple's tiles arrive inside MapKit JS")]

# ---- part 3 --------------------------------------------------------------------------------------------------
story += [PageBreak(), P("3. Address search and Locate Me: Photon and Apple Maps", "h2"),
          P(f"Post ride, edit ride and search all use this. {ST['addresses']} public buildings across 10 provinces and 2 territories, plus messy input, typing, and phone-GPS cases. Photon and OpenStreetMap's own Nominatim were called through their public APIs.")]
story += checks_block(3, "today", "TODAY (PHOTON)") + checks_block(3, "apple", "APPLE MAPS")
story += [Spacer(1, 8),
          pair("real-post-ride-type.png", "real post-ride form: typing 100 queen puts a Virginia university first", "t3-type-toronto-apple.png", "Apple test page: same typing, right address first"),
          pair("real-post-ride-locate.png", "Locate Me on the real post-ride page, fake GPS at downtown Toronto", "t3-locate-toronto-apple.png", "Apple at the same point: the landmark name, no door number"),
          pair("t3-locate-wawa-photon.png", "rural highway near Wawa, ON", "t3-locate-wawa-apple.png", "the same rural point"),
          pair("t3-locate-buffalo-photon.png", "Buffalo, NY: Photon accepts a US point", "t3-locate-buffalo-apple.png", "Buffalo, NY: the Apple node refuses (Canada only)")]
mrows = [[P("Case", "cellb"), P("Apple Maps", "cellb"), P("Photon", "cellb")]]
for m in S["messy"]:
    mrows.append([Paragraph(f"<b>{t(m['label'])}</b><br/><font size=7 color='#55656f'>{t(m['text'])}</font>", st["cell"]),
                  [status_cell(m["apple"]), Paragraph(t(m["apple_first"] or "no result"), st["sub"])], [status_cell(m["photon"]), Paragraph(t(m["photon_first"] or ""), st["sub"])]])
story += [P("Messy input, case by case", "h3"), table(mrows, [2.5 * inch, 2.3 * inch, 2.3 * inch])]

# ---- part 4 --------------------------------------------------------------------------------------------------
story += [PageBreak(), P("4. Routes, distance and drive time: OSRM and Apple Directions", "h2"),
          P("Used for the drive time stored on a ride and the line drawn on the map. Seven routes, one with a stop, three driver-detour riders.")]
story += checks_block(4, "today", "TODAY (OSRM DEMO SERVER)") + checks_block(4, "apple", "APPLE MAPS")
rr = [[P(h, "cellb") for h in ("Route", "Apple", "OSRM", "Distance gap", "Line agreement")]]
for r in T4["routes"]:
    rr.append([Paragraph(f"<b>{t(r['from'])}</b> to {t(r['to'])}<br/><font size=7 color='#55656f'>{t(r['kind'])}</font>", st["cell"]), P(f"{r['apple_km']} km / {r['apple_min']} min", "cell"), P(f"{r['osrm_km']} km / {r['osrm_min']} min", "cell"),
               P(f"{r['km_diff_pct']:+.0f}% km, {r['min_diff_pct']:+.0f}% time", "cell"), P(f"{r['line_gap']['within_200m_pct']}% within 200 m", "cell")])
dd = [[P(h, "cellb") for h in ("Driver detour for a rider", "Apple matrix", "Apple route + stop", "OSRM", "30 km rule")]]
for d in T4["detour"]:
    dd.append([Paragraph(f"<b>{t(d['rider'])}</b><br/><font size=7 color='#55656f'>{t(d['why'])}</font>", st["cell"]), P(f"{d['matrix_km']} km / {d['matrix_min']} min", "cell"), P(f"{d['route_km']} km / {d['route_min']} min", "cell"),
               P(f"{d['osrm_km']} km / {d['osrm_min']} min", "cell"), status_cell("fail" if d["over_30km_apple"] else "pass")])
story += [Spacer(1, 6), table(rr, [2.3 * inch, 1.15 * inch, 1.15 * inch, 1.3 * inch, 1.2 * inch]), Spacer(1, 8), table(dd, [2.2 * inch, 1.3 * inch, 1.3 * inch, 1.3 * inch, 1.0 * inch]), Spacer(1, 8),
          pair("t1-leaflet-desktop.png", "line from OSRM, 459.9 km", "t1-mapkit-desktop.png", "line from Apple Directions, 462.8 km")]

# ---- removal plan, speed ---------------------------------------------------------------------------------------
lat, lat4 = T3["latency_ms"], T4["latency_ms"]
sp = [[P(h, "cellb") for h in ("Step", "Today", "Apple Maps")],
      [P("Address autocomplete"), P(f"{lat['photon_search']['median']} ms"), P(f"{lat['apple_suggest']['median']} ms")], [P("Locate Me (reverse)"), P(f"{lat['photon_reverse']['median']} ms"), P(f"{lat['apple_reverse']['median']} ms")],
      [P("One route"), P(f"{lat4['osrm_route']['median']} ms (OSRM)"), P(f"{lat4['apple_route']['median']} ms")]]
story += [P("Speed", "h2"), table([[Paragraph(t(c.text), st["cell"]) if hasattr(c, "text") else c for c in r] for r in sp] if False else sp, [2.8 * inch, 2.15 * inch, 2.15 * inch]),
          P("Medians from this machine, one call at a time. Apple was called through a node on the same machine, so a deployed node adds one network hop.", "sub")]
story += [PageBreak(), P("Removing Photon: the plan, in order", "h2")]
for head, body in [
    ("0. Settle two questions first", "Does Apple's license (Schedule 6 of the Developer Program License Agreement) let Komute store ride coordinates and show results on a non-Apple map? Does Komute stay US and Canada, or Canada only? Komute's service_area.py also refuses Quebec pickups, stops and drop-offs."),
    ("1. Komute's own endpoints", "Add suggest, details and reverse views in Django that call the map node with the shared key (the same pattern as komute/services/verification.py), with rate limiting. The browser never sees an Apple key."),
    ("2. Swap the two Photon uses", "static/js/place-autocomplete.js (autocomplete on post-ride, edit-ride and search-rides) and the two Locate Me blocks, in post-ride.html and search-rides.html. Send the rider's location with every autocomplete call."),
    ("3. Tidy the edges", "Remove photon.komoot.io from the content security policy in komute/middleware.py. Rewrite the privacy policy lines that name Komoot. Update the regression tests that assert those hosts."),
    ("4. Ship and watch", "Run the node as the komute-map service, set MAP_API_URL and MAP_API_KEY, alert at 80% of Apple's 25,000 service calls a day, and ask Apple for more if growth needs it."),
]:
    story += [P(head, "h3"), P(body)]
story += [P("Quota to plan for", "h3"), P("Apple allows 25,000 service calls a day per team, shared by search and the browser map. Typing one address costs roughly 3 to 6 calls (an estimate from the 300 ms typing delay, not measured), so about 2,000 searches a day. The node caches repeat queries for 5 minutes.")]

# ---- appendix --------------------------------------------------------------------------------------------------
story += [PageBreak(), P("Appendix A. All typed addresses", "h2")]
ar = [[P(h, "cellb") for h in ("Address typed", "Apple geocode", "Photon", "OSM")]]
for a in T3["addresses"]:
    ar.append([Paragraph(f"<b>{t(a['name'])}</b><br/><font size=7 color='#55656f'>{t(a['query'])}</font>", st["cell"]), [status_cell("pass" if a["apple"] and a["apple"]["ok"] else "fail"), Paragraph(t(a["apple"]["title"]) if a["apple"] else "no result", st["sub"])],
               status_cell("pass" if a["photon"].get("ok") else "fail"), status_cell("pass" if a["osm"].get("ok") else "fail")])
story += [table(ar, [3.1 * inch, 2.4 * inch, 0.8 * inch, 0.8 * inch])]
story += [P("Appendix B. Method and limits", "h2"),
          P("One computer, one Sunday afternoon, no weekday traffic. The 'gap to OSM' and Locate Me door checks use OpenStreetMap points as the reference, which favours Photon. My expected house numbers come from my own knowledge of the buildings. Not tested: units and apartments, rural Quebec, entrance-level access, a real phone's GPS drift, Apple's and OSM's terms beyond what was read, or any change to Komute's real pages."),
          P("'Today' pictures are Komute's real pages on a throwaway SQLite database (seed_real_pages.py), driven by headless Chrome over the DevTools protocol (no browser-automation package installed). 'Apple' pictures are standalone test pages calling the map node. Scripts: t1_map_display.mjs, t2_tiles.py, t3_search.py, t3_ui.mjs, t3_rules.py, t4_routes.py, real_pages.mjs, summarize.py.")]

DEST.parent.mkdir(exist_ok=True)
doc = SimpleDocTemplate(str(DEST), pagesize=letter, leftMargin=0.7 * inch, rightMargin=0.7 * inch, topMargin=0.65 * inch, bottomMargin=0.75 * inch, title="Komute map stack test report", author="Komute")
doc.build(story, onFirstPage=footer, onLaterPages=footer)
print("pdf written:", DEST, round(DEST.stat().st_size / 1024), "KB")
