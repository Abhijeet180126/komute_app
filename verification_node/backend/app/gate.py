"""One verification at a time (a memory guard, not a rate limit), and the queue requests wait in for their turn."""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from typing import Optional

log = logging.getLogger("komute.queue")
POLL_S = 0.05


class RunGate:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        return self._lock.acquire(blocking=False)

    async def acquire(self, wait_s: float) -> bool:
        """Waits up to wait_s for the running check to finish. Polls, so the event loop is never blocked."""
        deadline = time.monotonic() + wait_s
        while not self._lock.acquire(blocking=False):
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(POLL_S)
        return True

    def release(self) -> None:
        self._lock.release()

    def busy(self) -> bool:
        return self._lock.locked()


class LocalQueue:
    """No Redis configured: wait on the gate alone. One at a time still holds; arrival order is not guaranteed."""

    backend = "local"

    def __init__(self, gate: RunGate) -> None:
        self._gate = gate

    async def acquire(self, wait_s: float) -> Optional[str]:
        return "local" if await self._gate.acquire(wait_s) else None

    async def release(self, job: str) -> None:
        self._gate.release()

    async def waiting(self) -> Optional[int]:
        return None

    async def reset(self) -> None:
        return None


class RedisQueue:
    """First come, first served. Each request joins a Redis list and runs when its id reaches the front; the gate still
    makes sure only one check runs. Redis holds only random job ids: photos and licence data never leave this process.

    A request that gives up or whose caller hangs up leaves the line. One that died without leaving (a crash) stops
    renewing its alive key, and the next request clears it from the front. If Redis fails, the request falls back to
    the gate alone rather than refusing the driver.
    """

    backend = "redis"
    ALIVE_S = 30          # renewed while waiting; a dead request is cleared this long after it stopped
    RUNNING_S = 300       # while its check runs, longer than any check (the barcode budget is at most 60 s)

    def __init__(self, client, gate: RunGate, key: str = "komute:verify:queue") -> None:
        self._r, self._gate, self._key = client, gate, key

    def _alive(self, job: str) -> str:
        return f"{self._key}:alive:{job}"

    async def acquire(self, wait_s: float) -> Optional[str]:
        deadline = time.monotonic() + wait_s
        job = uuid.uuid4().hex
        try:
            await self._r.set(self._alive(job), 1, ex=self.ALIVE_S)
            await self._r.rpush(self._key, job)
            while True:
                head = await self._r.lindex(self._key, 0)
                if head == job and self._gate.try_acquire():
                    await self._r.expire(self._alive(job), self.RUNNING_S)
                    return job
                if head not in (None, job) and not await self._r.exists(self._alive(head)):
                    await self._r.lrem(self._key, 1, head)  # died without leaving the line
                    continue
                if time.monotonic() >= deadline:
                    await self._leave(job)
                    return None
                await self._r.expire(self._alive(job), self.ALIVE_S)
                await asyncio.sleep(POLL_S)
        except asyncio.CancelledError:
            await self._leave_quietly(job)
            raise
        except Exception as e:  # Redis lost: keep verifying, one at a time, without the shared line
            log.warning("verification queue: Redis unavailable (%s); using the local gate", type(e).__name__)
            await self._leave_quietly(job)
            return "local" if await self._gate.acquire(max(0.0, deadline - time.monotonic())) else None

    async def release(self, job: str) -> None:
        self._gate.release()
        if job != "local":
            await self._leave_quietly(job)

    async def waiting(self) -> Optional[int]:
        try:
            return int(await self._r.llen(self._key))
        except Exception:
            return None

    async def reset(self) -> None:
        """At start-up: this node is the only one serving the line, and a restarted node has no live requests."""
        try:
            await self._r.delete(self._key)
        except Exception as e:
            log.warning("verification queue: could not clear the line at start-up (%s)", type(e).__name__)

    async def _leave(self, job: str) -> None:
        await self._r.lrem(self._key, 1, job)
        await self._r.delete(self._alive(job))

    async def _leave_quietly(self, job: str) -> None:
        try:
            await self._leave(job)
        except Exception:
            pass  # Redis is down; the alive key expires on its own and the next request clears the entry
