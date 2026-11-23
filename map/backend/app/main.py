"""FastAPI wrapper. Run with:  uvicorn backend.app.main:create_app --factory --workers 1

Stores nothing. Holds the Apple Maps key; the caller (the main app) holds only MAP_API_KEY.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.core.cache import TTLCache
from backend.core.config import Settings, get_settings
from backend.core.errors import UpstreamError
from backend.core.providers.apple import AppleMaps

from .errors import ApiError, error_response
from .routes import public, router

log = logging.getLogger("komute.map.api")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


def create_app(settings: Settings | None = None, provider: object | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Komute-Map", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.cache = TTLCache(settings.cache_max_entries)
    if provider is None and settings.apple_ready:
        provider = AppleMaps(settings.apple_team_id, settings.apple_key_id, settings.apple_key_pem(), settings.apple_web_origin,
                             timeout=(settings.connect_timeout_s, settings.read_timeout_s),
                             countries=settings.countries, default_bias=settings.default_bias_point)
    app.state.provider = provider
    if settings.api_key is None:
        log.warning("authentication is OFF: no MAP_API_KEY set (local testing only)")
    if provider is None:
        log.warning("Apple Maps credentials are not set: every map call will answer 503 NOT_CONFIGURED")
    app.include_router(router)
    app.include_router(public)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, e: ApiError) -> JSONResponse:
        return error_response(e.status, e.code, e.message, e.headers)

    @app.exception_handler(UpstreamError)
    async def _upstream_error(_: Request, e: UpstreamError) -> JSONResponse:
        headers = {"Retry-After": str(e.retry_after or settings.retry_after_s)} if e.status == 503 else None
        return error_response(e.status, e.code, e.message, headers)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, e: RequestValidationError) -> JSONResponse:
        # Names the bad fields only. Never echoes the submitted values back.
        fields = sorted({".".join(str(p) for p in err["loc"][1:]) or str(err["loc"][0]) for err in e.errors()})
        return error_response(422, "INVALID_FIELD", "Invalid or missing: " + ", ".join(fields) + ".")

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, e: Exception) -> JSONResponse:
        log.error("unhandled type=%s", type(e).__name__)
        return error_response(500, "INTERNAL_ERROR", "The map request could not be completed.")

    return app
