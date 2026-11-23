"""Part 3, service-area rules: what the node does with "Locate Me" points outside Canada, and inside Quebec.
Writes qa/out/t3_rules.json.  python qa/t3_rules.py   (4 Apple calls)
Komute's own rule refusing Quebec pickups lives in komute/service_area.py; the node does not repeat it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.core.config import Settings

app = TestClient(create_app(Settings(_env_file=HERE.parents[1] / ".env", api_key=None)))
POINTS = [("Buffalo, NY (US, inside the node's coordinate box)", 42.8864, -78.8784, "refused"), ("Seattle, WA (US, inside the box)", 47.6062, -122.3321, "refused"),
          ("Windsor, ON (Canada, next to Detroit)", 42.3149, -83.0364, "answered"), ("Montréal, QC (Canada, Quebec)", 45.5017, -73.5673, "answered by the node; Komute's service_area.py refuses Quebec")]
rows = []
for label, lat, lon, expect in POINTS:
    r = app.get("/v1/places/reverse", params={"lat": lat, "lon": lon})
    j = r.json()
    rows.append({"label": label, "expect": expect, "status": r.status_code, "answer": (j.get("label") or j.get("title")) if r.status_code == 200 else j["error"]["code"]})
(HERE / "out" / "t3_rules.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf8")
for r in rows:
    print(r["status"], r["label"], "->", r["answer"])
