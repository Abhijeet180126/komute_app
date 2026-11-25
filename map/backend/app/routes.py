from __future__ import annotations

from typing import Annotated, Any, Callable

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .errors import ApiError
from .schemas import REF_PATTERN, Lat, Limit, Lon, MatrixIn, QueryText, RouteIn
from .security import require_api_key

router = APIRouter(dependencies=[Depends(require_api_key)])
public = APIRouter()


def _provider(request: Request):
    provider = request.app.state.provider
    if provider is None:
        raise ApiError(503, "NOT_CONFIGURED", "The map service has no provider key set.")
    return provider


def _pair(lat: float | None, lon: float | None) -> tuple[float | None, float | None]:
    if (lat is None) != (lon is None):
        raise ApiError(422, "INVALID_FIELD", "Send lat and lon together or not at all.")
    return lat, lon


async def _cached(request: Request, key: tuple, ttl_s: int, fn: Callable[[], Any]) -> Any:
    cache = request.app.state.cache
    hit = cache.get(key)
    if hit is not None:
        return hit
    value = await run_in_threadpool(fn)
    cache.set(key, value, ttl_s)
    return value


@router.get("/v1/places/suggest")
async def suggest(request: Request, q: QueryText, lat: Lat | None = None, lon: Lon | None = None,
                  limit: Limit = 5) -> JSONResponse:
    """Address autocomplete. Results carry a `ref`: pass it to /v1/places/details when the user picks one."""
    lat, lon = _pair(lat, lon)
    p, s = _provider(request), request.app.state.settings
    q = q.strip()
    if not q:
        raise ApiError(422, "INVALID_FIELD", "q must not be blank.")
    key = ("suggest", q.casefold(), None if lat is None else round(lat, 2), None if lon is None else round(lon, 2), limit)
    results = await _cached(request, key, s.suggest_ttl_s, lambda: p.suggest(q, lat, lon, limit))
    return JSONResponse({"results": results})


@router.get("/v1/places/details")
async def details(request: Request, ref: Annotated[str, Query(pattern=REF_PATTERN)]) -> JSONResponse:
    """The chosen suggestion with coordinates and structured address (house number, street, city, province, postal code)."""
    p, s = _provider(request), request.app.state.settings
    place = await _cached(request, ("details", ref), s.place_ttl_s, lambda: p.details(ref))
    if place is None:
        raise ApiError(404, "NOT_FOUND", "That place has no location.")
    return JSONResponse(place)


@router.get("/v1/places/geocode")
async def geocode(request: Request, q: QueryText, lat: Lat | None = None, lon: Lon | None = None,
                  limit: Limit = 5) -> JSONResponse:
    """Free-text address to coordinates."""
    lat, lon = _pair(lat, lon)
    p, s = _provider(request), request.app.state.settings
    q = q.strip()
    if not q:
        raise ApiError(422, "INVALID_FIELD", "q must not be blank.")
    key = ("geocode", q.casefold(), None if lat is None else round(lat, 2), None if lon is None else round(lon, 2), limit)
    results = await _cached(request, key, s.place_ttl_s, lambda: p.geocode(q, lat, lon, limit))
    return JSONResponse({"results": results})


@router.get("/v1/places/reverse")
async def reverse(request: Request, lat: Lat, lon: Lon) -> JSONResponse:
    """Coordinates to the nearest address ("Locate Me", dragged pin)."""
    p, s = _provider(request), request.app.state.settings
    place = await _cached(request, ("reverse", round(lat, 4), round(lon, 4)), s.place_ttl_s, lambda: p.reverse(lat, lon))
    if place is None:
        raise ApiError(404, "NOT_FOUND", "No address was found near that point.")
    # The coordinate box is loose (it also holds Mexico and the Caribbean). The country Apple returns decides.
    country = (place.get("address") or {}).get("country")
    if country is not None and country not in s.countries_list:
        raise ApiError(422, "OUTSIDE_SERVICE_AREA", "That point is outside the service area (" + " and ".join(s.countries_list) + ").")
    return JSONResponse(place)


@router.post("/v1/route")
async def route(request: Request, body: RouteIn) -> JSONResponse:
    """Drive route with live traffic: distance, duration, per-leg figures and the line to draw. Not cached."""
    p, s = _provider(request), request.app.state.settings
    if len(body.waypoints) > s.max_waypoints:
        raise ApiError(422, "TOO_MANY_WAYPOINTS", f"At most {s.max_waypoints} waypoints are allowed.")
    origin, dest = body.origin.model_dump(), body.destination.model_dump()
    waypoints = [w.model_dump() for w in body.waypoints]
    return JSONResponse(await run_in_threadpool(p.route, origin, dest, waypoints))


@router.post("/v1/matrix")
async def matrix(request: Request, body: MatrixIn) -> JSONResponse:
    """Drive time and distance for every origin to every destination. `rows[i][j]` is null when there is no route."""
    p, s = _provider(request), request.app.state.settings
    if len(body.origins) > s.max_matrix_origins:
        raise ApiError(422, "MATRIX_TOO_LARGE", f"At most {s.max_matrix_origins} origins are allowed.")
    if len(body.origins) * len(body.destinations) > s.max_matrix_cells:
        raise ApiError(422, "MATRIX_TOO_LARGE", f"origins x destinations must not exceed {s.max_matrix_cells}.")
    origins, dests = [o.model_dump() for o in body.origins], [d.model_dump() for d in body.destinations]
    return JSONResponse({"rows": await run_in_threadpool(p.matrix, origins, dests)})


@router.get("/v1/mapkit-token")
async def mapkit_token(request: Request) -> JSONResponse:
    """Short-lived token the browser map (MapKit JS) needs, locked to our domain. The main app passes it to the page."""
    p, s = _provider(request), request.app.state.settings
    if s.apple_web_origin is None:
        raise ApiError(503, "NOT_CONFIGURED", "The map service has no web origin set.")
    token, ttl = await run_in_threadpool(p.mapkit_token)
    return JSONResponse({"token": token, "expires_in": ttl})


@public.get("/health")
async def health(request: Request) -> JSONResponse:
    ok = request.app.state.provider is not None
    return JSONResponse({"status": "ok" if ok else "degraded", "provider_configured": ok}, status_code=200 if ok else 503)
