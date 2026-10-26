"""
Quebec is outside the service area at launch (audit P-11 / D-5): Bill 96
requires French Terms, Privacy Policy and signup for Quebec consumers, and
Komüte is English-only for now. Pickups, stops and drop-offs in Quebec are
refused; rides that only drive through Quebec are fine.

data/quebec.json is Quebec's land area as a list of outer rings of
[lng, lat] points, built from geoBoundaries CAN ADM1 (Statistics Canada,
Open Government Licence - Canada), islands under 0.1 km2 dropped and edges
simplified to about 200 m. Rivers and the St. Lawrence are outside every
ring, so a point on the water between Ottawa and Gatineau counts as outside
Quebec; lakes inside Quebec count as Quebec.
"""
import json
import os

_rings = None


def _load():
    global _rings
    if _rings is None:
        path = os.path.join(os.path.dirname(__file__), "data", "quebec.json")
        with open(path, encoding="utf-8") as f:
            rings = json.load(f)
        _rings = [
            ((min(x for x, _ in r), min(y for _, y in r), max(x for x, _ in r), max(y for _, y in r)), r)
            for r in rings
        ]
    return _rings


def _in_ring(x, y, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def in_quebec(lat, lng):
    for (min_x, min_y, max_x, max_y), ring in _load():
        if min_x <= lng <= max_x and min_y <= lat <= max_y and _in_ring(lng, lat, ring):
            return True
    return False
