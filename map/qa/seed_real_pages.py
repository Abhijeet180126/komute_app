"""Runs Komute's REAL pages on a throwaway SQLite database, for the "before" screenshots (today's Leaflet, Photon, OSRM).
Run from anywhere:  python map/qa/seed_real_pages.py     It prints the session cookie to qa/out/real_pages.json and serves on :8770.
Nothing is written outside map/qa/out. The real .env and database are never read.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
os.environ["DJANGO_SETTINGS_MODULE"] = "qa_settings"

import django  # noqa: E402

django.setup()
from django.core.management import call_command  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402

DB = HERE / "out" / "qa_db.sqlite3"
if DB.exists():
    DB.unlink()
call_command("migrate", verbosity=0, interactive=False)

from komute import tests_regression as h  # noqa: E402  (its helpers build users, a vehicle and a ride exactly as the tests do)
from komute.hashid_utils import encode_id  # noqa: E402
from komute.models import RideStop  # noqa: E402

driver = h.make_driver_with_vehicle("qa_driver@example.com")
rider = h.make_user("qa_rider@example.com")
ride = h.make_ride(driver, origin="Toronto City Hall, 100 Queen St W", origin_lat=43.6528, origin_lng=-79.3836,
                   destination="Parliament Hill, 111 Wellington St, Ottawa", dest_lat=45.4236, dest_lng=-75.7009)
RideStop.objects.create(ride=ride, stop_order=1, location="Kingston City Hall, 216 Ontario St", lat=44.2312, lng=-76.4860)
# ride-summary opens only for completed rides, so the same trip exists twice: one completed (summary), one open (booking page)
done = h.make_ride(driver, origin=ride.origin, origin_lat=ride.origin_lat, origin_lng=ride.origin_lng,
                   destination=ride.destination, dest_lat=ride.dest_lat, dest_lng=ride.dest_lng, status="completed")
RideStop.objects.create(ride=done, stop_order=1, location="Kingston City Hall, 216 Ontario St", lat=44.2312, lng=-76.4860)


def session_cookie(user) -> str:
    c = Client()
    c.force_login(user)  # login() would go through django-axes, which wants a real request
    return c.cookies["sessionid"].value


info = {"base": "http://127.0.0.1:8770", "ride_id": encode_id(ride.id),
        "cookies": {"driver": session_cookie(driver), "rider": session_cookie(rider)},
        "pages": {"post-ride": reverse("post-ride"), "search-rides": reverse("search-rides"),
                  "ride-summary": reverse("ride-summary", args=[encode_id(done.id)]),
                  "booking-details": reverse("create-ride-booking", args=[encode_id(ride.id)])}}
(HERE / "out" / "real_pages.json").write_text(json.dumps(info, indent=1), encoding="utf8")
print("seeded; serving on http://127.0.0.1:8770", flush=True)
call_command("runserver", "127.0.0.1:8770", "--noreload", verbosity=0)
