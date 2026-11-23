from __future__ import annotations


class UpstreamError(Exception):
    """An upstream map call failed. `message` is safe to show to the caller: it never holds the URL, key or upstream text."""

    def __init__(self, status: int, code: str, message: str, retry_after: int | None = None) -> None:
        super().__init__(code)
        self.status, self.code, self.message, self.retry_after = status, code, message, retry_after
