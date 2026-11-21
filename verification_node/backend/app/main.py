"""FastAPI wrapper. Run with:  uvicorn backend.app.main:create_app --factory --workers 1

Stores nothing: photos and licence data live in memory for one request. The caller keeps the outcome.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.formparsers import MultiPartParser

from backend.core.config import Settings, get_settings
from backend.core.monitor import SystemSampler
from backend.core.contracts import EngineError, InputError
from backend.core.imaging import IMAGE_KEYS
from backend.core.pipeline import Engine, build_engine

from .errors import ApiError, error_response
from .compat import router as compat_router
from .gate import RunGate
from .pages import STATIC, router as pages_router
from .routes import router

log = logging.getLogger("komute.api")

# The test page is one file with inline script and style, so those two are allowed; nothing else loads from anywhere.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data: blob:; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
}


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or get_settings()
    # Starlette writes an upload part bigger than this to a temporary file on disk. Photos must never reach disk, so
    # the limit is the whole body limit that _read_form enforces: no accepted request spills.
    MultiPartParser.spool_max_size = len(IMAGE_KEYS) * settings.max_upload_bytes + 200_000

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if getattr(app.state, "engine", None) is None:
            app.state.engine = build_engine(settings)  # fails startup closed if models or settings are wrong
        app.state.sampler = SystemSampler(settings.models_dir).start()
        yield
        app.state.sampler.stop()

    app = FastAPI(title="Komute-Local", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.gate = RunGate()
    if settings.api_key is None:
        log.warning("authentication is OFF: no KOMUTE_API_KEY set (local testing only)")
    app.state.engine = engine
    app.include_router(router)
    app.include_router(compat_router)
    if STATIC.is_dir():  # absent in the copy that lives inside the main Komüte app
        app.include_router(pages_router)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, e: ApiError) -> JSONResponse:
        return error_response(e.status, e.code, e.message, e.headers)

    @app.exception_handler(InputError)
    async def _input_error(_: Request, e: InputError) -> JSONResponse:
        return error_response(422, e.code, "The image could not be read. Upload a JPEG, PNG or WebP photo.")

    @app.exception_handler(EngineError)
    async def _engine_error(_: Request, e: EngineError) -> JSONResponse:
        return error_response(500, "ENGINE_ERROR", "The verification could not be completed. No decision was made.")

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, e: Exception) -> JSONResponse:
        log.error("unhandled type=%s", type(e).__name__)
        return error_response(500, "ENGINE_ERROR", "The verification could not be completed. No decision was made.")

    return app
