from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from backend.core import hardware
from backend.core.imaging import IMAGE_KEYS

from .errors import ApiError
from .pages import stats_file
from .schemas import build_input
from .security import require_api_key

router = APIRouter()
FILE_FIELDS = {k: k for k in IMAGE_KEYS}


async def _read_form(request: Request, max_bytes: int) -> tuple[dict[str, str], dict[str, bytes]]:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > len(IMAGE_KEYS) * max_bytes + 200_000:
        raise ApiError(422, "IMAGE_TOO_LARGE", "The request is larger than the allowed upload size.")
    form = await request.form()
    fields: dict[str, str] = {}
    images: dict[str, bytes] = {}
    for key, value in form.multi_items():
        if isinstance(value, UploadFile):
            if key in FILE_FIELDS:
                data = await value.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ApiError(422, "IMAGE_TOO_LARGE", f"Image {key} is larger than {max_bytes} bytes.")
                images[key] = data
        else:
            fields[key] = value
    for k in IMAGE_KEYS:
        if k not in images or not images[k]:
            raise ApiError(422, "MISSING_FIELD", f"Image {k} is required.")
    return fields, images


@router.post("/v1/verify", dependencies=[Depends(require_api_key)])
async def verify(request: Request) -> JSONResponse:
    queue = request.app.state.queue
    job = await queue.acquire(request.app.state.settings.busy_wait_s)
    if job is None:
        raise ApiError(
            503, "BUSY", "A verification is already running.",
            {"Retry-After": str(request.app.state.settings.retry_after_s)},
        )
    try:
        fields, images = await _read_form(request, request.app.state.settings.max_upload_bytes)
        inp = build_input(fields, images, request.app.state.settings.max_text_chars)
        response = await run_in_threadpool(request.app.state.engine.verify, inp)
        return JSONResponse(response)
    finally:
        await queue.release(job)


@router.get("/health")
async def health(request: Request) -> JSONResponse:
    info = request.app.state.engine.health_info()
    loaded = info.get("models_loaded", {})
    required = [v for k, v in loaded.items() if k != "liveness"]
    return JSONResponse(
        {"status": "ok" if all(required) else "degraded", "models_loaded": loaded, "rss_mb": round(hardware.rss_mb(), 1),
         "queue": {"backend": request.app.state.queue.backend, "waiting": await request.app.state.queue.waiting()}},
        status_code=200 if all(required) else 503,
    )


_SECRET_FIELDS = {"api_key"}
_BUDGET_MB = {"steady_target": 900, "peak_limit": 1200}  # CLAUDE.md memory budget


@router.get("/v1/system", response_model=None)
async def system(request: Request) -> JSONResponse | FileResponse:
    """Live system and engine figures. Holds no personal data and no secrets.

    A browser gets the dashboard page when this install has one (backend/static); every other caller gets JSON.
    """
    page = stats_file() if "text/html" in request.headers.get("accept", "") else None
    if page is not None:
        return FileResponse(page, media_type="text/html", headers={"Cache-Control": "no-store"})
    require_api_key(request, request.headers.get("x-api-key"))
    s = request.app.state.settings
    sampler = getattr(request.app.state, "sampler", None)
    engine = request.app.state.engine
    activity = engine.monitor.snapshot()
    models = engine.models_info()
    for m in models:
        m["status"] = ("running" if activity["busy"] and activity["stage"] == m["stage"] else "idle") if m["loaded"] else (
            "skipped (dev)" if m["role"] == "liveness" and s.dev_skip_liveness else "not loaded")
    settings_view = {k: (str(v) if not isinstance(v, (int, float, bool, type(None))) else v)
                     for k, v in s.model_dump().items() if k not in _SECRET_FIELDS}
    settings_view["authentication"] = s.api_key is not None
    return JSONResponse({
        "now": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "host": sampler.static if sampler else None,
        "sample": sampler.latest() if sampler else None,
        "history": sampler.history() if sampler else [],
        "budget_mb": _BUDGET_MB,
        "engine": {**activity, "gate_busy": request.app.state.gate.busy(), "started_at": engine.monitor.started_at.isoformat()},
        "models": models,
        "settings": settings_view,
    }, headers={"Cache-Control": "no-store"})
