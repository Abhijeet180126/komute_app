"""N3: do US addresses work through the node? 11 US public buildings across the country (incl. Alaska-adjacent and Hawaii),
sent with NO location, so the node's default bias is what helps. Writes qa/out/n3_us.json.  python qa/n3_us.py  (about 35 Apple calls)
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.core.config import Settings

app = TestClient(create_app(Settings(_env_file=HERE.parents[1] / ".env", api_key=None)))
ABBR = {"street": "st", "avenue": "ave", "road": "rd", "drive": "dr", "boulevard": "blvd", "place": "pl", "square": "sq", "west": "w", "east": "e", "north": "n", "south": "s",
        "first": "1st", "second": "2nd", "third": "3rd", "fifth": "5th", "sixth": "6th"}


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().casefold()
    return " ".join(ABBR.get(w, w) for w in re.sub(r"[^a-z0-9 ]+", " ", s).split())


def has(text, words):
    have = set(norm(text).split())
    # "LaSalle" and "La Salle" are the same street: also compare with the spaces removed
    return all(w in have for w in norm(words).split()) or norm(words).replace(" ", "") in norm(text).replace(" ", "")


# name, typed text, house number, street words, city
US = [
    ("White House", "1600 Pennsylvania Avenue NW, Washington, DC", "1600", "pennsylvania", "washington"),
    ("Empire State Building", "350 5th Avenue, New York, NY", "350", "5th", "new york"),
    ("Willis Tower", "233 S Wacker Drive, Chicago, IL", "233", "wacker", "chicago"),
    ("Space Needle", "400 Broad Street, Seattle, WA", "400", "broad", "seattle"),
    ("Transamerica Pyramid", "600 Montgomery Street, San Francisco, CA", "600", "montgomery", "san francisco"),
    ("San Francisco City Hall", "1 Dr Carlton B Goodlett Place, San Francisco, CA", "1", "goodlett", "san francisco"),
    ("Chicago City Hall", "121 N LaSalle Street, Chicago, IL", "121", "lasalle", "chicago"),
    ("Los Angeles City Hall", "200 N Spring Street, Los Angeles, CA", "200", "spring", "los angeles"),
    ("Boston City Hall", "1 City Hall Square, Boston, MA", "1", "city hall", "boston"),
    ("Houston City Hall", "901 Bagby Street, Houston, TX", "901", "bagby", "houston"),
    ("Honolulu Hale", "530 S King Street, Honolulu, HI", "530", "king", "honolulu"),
]
rows = []
for name, q, hn, street, city in US:
    g = app.get("/v1/places/geocode", params={"q": q, "limit": 3}).json().get("results", [])
    s = app.get("/v1/places/suggest", params={"q": q, "limit": 5}).json().get("results", [])  # no lat/lon on purpose
    p = g[0] if g else None
    a = (p or {}).get("address") or {}
    geo_ok = bool(p) and a.get("house_number") == hn and has(a.get("street") or p.get("title"), street) and has(f'{a.get("city")} {p.get("label")}', city)
    sg = [x for x in s if hn in norm(x["title"]).split() and has(x["title"], street) and has(x["subtitle"] + " " + x["title"], city.split()[0])]
    rows.append({"name": name, "typed": q, "geocode": p and p.get("label"), "geocode_ok": geo_ok, "suggest_top1": bool(s) and bool(sg) and s[0] is sg[0],
                 "suggest_top5": bool(sg), "suggest_first": (s[0]["title"] + " | " + s[0]["subtitle"]) if s else None, "country": a.get("country")})
rev = []
for label, lat, lon in (("Brooklyn, NY", 40.6782, -73.9442), ("Honolulu, HI", 21.3069, -157.8583), ("Anchorage, AK", 61.2181, -149.9003), ("Mexico City (outside)", 19.4326, -99.1332)):
    r = app.get("/v1/places/reverse", params={"lat": lat, "lon": lon})
    rev.append({"label": label, "status": r.status_code, "answer": (r.json().get("label") if r.status_code == 200 else r.json()["error"]["code"])})
out = {"addresses": rows, "reverse": rev,
       "summary": {"n": len(rows), "geocode_ok": sum(r["geocode_ok"] for r in rows), "suggest_top1": sum(r["suggest_top1"] for r in rows), "suggest_top5": sum(r["suggest_top5"] for r in rows)}}
(HERE / "out" / "n3_us.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf8")
print(out["summary"])
for r in rows:
    if not (r["geocode_ok"] and r["suggest_top1"]):
        print("  check:", r["name"], "| geocode:", r["geocode"], "| first suggestion:", r["suggest_first"])
for r in rev:
    print("  reverse", r["label"], r["status"], r["answer"])
