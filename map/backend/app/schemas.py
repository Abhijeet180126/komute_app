"""Request shapes. Coordinates are limited to a loose box around the US and Canada (Komute's own boxes in komute/services/geo.py are the real gate)."""
from __future__ import annotations

from typing import Annotated

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field

LAT_MIN, LAT_MAX, LON_MIN, LON_MAX = 18.0, 84.0, -170.0, -52.0  # covers Canada, the US, Alaska and Hawaii
# Exactly as /v1/places/suggest returns it in `ref`: URL-safe base64, opaque to the caller. The provider re-checks it.
REF_PATTERN = r"^[A-Za-z0-9_-]{8,2100}$"

Lat = Annotated[float, Field(ge=LAT_MIN, le=LAT_MAX)]
Lon = Annotated[float, Field(ge=LON_MIN, le=LON_MAX)]
QueryText = Annotated[str, Query(min_length=1, max_length=200)]
Limit = Annotated[int, Query(ge=1, le=10)]


class Point(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lat: Lat
    lon: Lon


class RouteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: Point
    destination: Point
    waypoints: list[Point] = Field(default_factory=list)


class MatrixIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origins: list[Point] = Field(min_length=1)
    destinations: list[Point] = Field(min_length=1)
