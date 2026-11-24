from __future__ import annotations

import hmac

from fastapi import Header, Request

from .errors import ApiError


def require_api_key(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    """Always enforced. Settings refuse to load without a key; a missing one here still refuses the call."""
    key = request.app.state.settings.api_key
    if key is None or x_api_key is None or not hmac.compare_digest(x_api_key.encode(), key.get_secret_value().encode()):
        raise ApiError(401, "UNAUTHORIZED", "A valid X-API-Key header is required.")
