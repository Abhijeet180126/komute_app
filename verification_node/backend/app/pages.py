"""The test page and the system dashboard. Served only when backend/static exists.

The copy of the node inside the main Komüte app has no backend/static folder, so there the routes are not added.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, Response

from .errors import ApiError

STATIC = Path(__file__).resolve().parent.parent / "static"
STATIC_STATS = STATIC / "stats.html"
router = APIRouter()


def stats_file() -> Path | None:
    return STATIC_STATS if STATIC_STATS.exists() else None


def _page(name: str) -> FileResponse:
    page = STATIC / name
    if not page.exists():
        raise ApiError(404, "NOT_FOUND", "Page not installed.")
    return FileResponse(page, media_type="text/html", headers={"Cache-Control": "no-store"})  # a stale page hid new features


@router.get("/stats", include_in_schema=False)
@router.get("/system-stats", include_in_schema=False)
async def stats_page() -> FileResponse:
    return _page("stats.html")


@router.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=204)


@router.get("/")
async def index() -> FileResponse:
    return _page("index.html")
