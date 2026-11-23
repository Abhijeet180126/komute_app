"""Serves the qa test pages and the map node on one local port, so the pages can call /api/map/* like Komute's pages will.

Run:  python qa/lab_server.py            (from the map/ folder, 127.0.0.1:8765)
The node runs inside this process with the MapKit web origin set to this server, so the browser token matches.
Local only: it binds to 127.0.0.1 and skips MAP_API_KEY. The real Django proxy will add that key server-side.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # so `backend` imports resolve when run from anywhere

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend.app.main import create_app
from backend.core.config import Settings

PORT = int(os.environ.get("LAB_PORT", "8765"))
ORIGIN = os.environ.get("LAB_ORIGIN", f"http://localhost:{PORT}")


def build() -> FastAPI:
    node = create_app(Settings(_env_file=HERE.parents[1] / ".env", apple_web_origin=ORIGIN, api_key=None))  # Apple credentials come from the repo-root .env
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/api/map", node)
    app.mount("/lab", StaticFiles(directory=HERE / "lab", html=True), name="lab")
    app.mount("/prod-static", StaticFiles(directory=HERE.parents[1] / "static"), name="prod-static")  # read-only: the real production JS
    app.mount("/vendor", StaticFiles(directory=HERE / "vendor"), name="vendor") if (HERE / "vendor").is_dir() else None
    return app


if __name__ == "__main__":
    uvicorn.run(build(), host="127.0.0.1", port=PORT, log_level="warning")
