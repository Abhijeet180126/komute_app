"""The verification queue: requests run one at a time, oldest first; Redis holds only job ids (never photos)."""
import asyncio
import uuid

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from backend.app.gate import LocalQueue, RedisQueue, RunGate

KEY = "test:verify:queue"


class FakeRedis:
    """The few async Redis commands the queue uses, in memory. `down=True` makes every call fail like a lost server."""

    def __init__(self, down=False):
        self.lists, self.keys, self.down = {}, set(), down

    def _check(self):
        if self.down:
            raise RedisConnectionError("Redis is down")

    async def rpush(self, key, value):
        self._check()
        self.lists.setdefault(key, []).append(value)

    async def lindex(self, key, i):
        self._check()
        items = self.lists.get(key, [])
        return items[i] if len(items) > i else None

    async def lrem(self, key, count, value):
        self._check()
        if value in self.lists.get(key, []):
            self.lists[key].remove(value)

    async def llen(self, key):
        self._check()
        return len(self.lists.get(key, []))

    async def set(self, key, value, ex=None):
        self._check()
        self.keys.add(key)

    async def expire(self, key, seconds):
        self._check()

    async def exists(self, key):
        self._check()
        return int(key in self.keys)

    async def delete(self, *keys):
        self._check()
        for k in keys:
            self.keys.discard(k)
            self.lists.pop(k, None)


def run(coro):
    return asyncio.run(coro)


def test_requests_run_one_at_a_time_in_arrival_order():
    async def main():
        q, order = RedisQueue(FakeRedis(), RunGate(), key=KEY), []

        async def verify(name, hold):
            job = await q.acquire(5.0)
            order.append(name)
            await asyncio.sleep(hold)
            await q.release(job)

        tasks = []
        for name in ("first", "second", "third"):
            tasks.append(asyncio.create_task(verify(name, 0.1)))
            await asyncio.sleep(0.02)  # arrive in this order
        await asyncio.gather(*tasks)
        return order, await q.waiting()

    order, left = run(main())
    assert order == ["first", "second", "third"] and left == 0


def test_a_request_that_waits_too_long_leaves_the_line():
    async def main():
        r = FakeRedis()
        q = RedisQueue(r, RunGate(), key=KEY)
        first = await q.acquire(1.0)
        late = await q.acquire(0.2)
        waiting = await q.waiting()
        await q.release(first)
        return first, late, waiting, await q.waiting()

    first, late, waiting, after = run(main())
    assert first and late is None and waiting == 1 and after == 0


def test_a_request_that_died_at_the_front_does_not_block_the_line():
    async def main():
        r = FakeRedis()
        await r.rpush(KEY, "ghost")  # in the line, but its alive key expired (the process crashed mid-check)
        q = RedisQueue(r, RunGate(), key=KEY)
        job = await q.acquire(1.0)
        await q.release(job)
        return job, r.lists.get(KEY)

    job, line = run(main())
    assert job and line == []


def test_a_cancelled_request_leaves_the_line():
    async def main():
        q = RedisQueue(FakeRedis(), RunGate(), key=KEY)
        first = await q.acquire(1.0)
        waiter = asyncio.create_task(q.acquire(5.0))
        await asyncio.sleep(0.1)
        waiter.cancel()  # the caller hung up
        with pytest.raises(asyncio.CancelledError):
            await waiter
        left = await q.waiting()
        await q.release(first)
        return left

    assert run(main()) == 1


def test_redis_down_falls_back_to_the_local_line():
    async def main():
        q = RedisQueue(FakeRedis(down=True), RunGate(), key=KEY)
        first = await q.acquire(1.0)
        second = await q.acquire(0.1)  # one at a time still holds without Redis
        await q.release(first)
        third = await q.acquire(0.1)
        await q.release(third)
        return first, second, third, await q.waiting()

    first, second, third, waiting = run(main())
    assert first and second is None and third and waiting is None  # length unknown while Redis is down


def test_reset_clears_the_line_at_startup():
    async def main():
        r = FakeRedis()
        await r.rpush(KEY, "left-over")
        await RedisQueue(r, RunGate(), key=KEY).reset()
        return r.lists.get(KEY)

    assert run(main()) is None


def test_local_queue_without_redis():
    async def main():
        q = LocalQueue(RunGate())
        first = await q.acquire(1.0)
        second = await q.acquire(0.1)
        await q.release(first)
        return first, second, await q.waiting()

    first, second, waiting = run(main())
    assert first and second is None and waiting is None


def test_on_a_real_redis_server():
    redis_asyncio = pytest.importorskip("redis.asyncio")

    async def main():
        client = redis_asyncio.from_url("redis://127.0.0.1:6379/0", decode_responses=True, socket_connect_timeout=1)
        try:
            await client.ping()
        except Exception:
            pytest.skip("no Redis server on 127.0.0.1:6379")
        key = f"test:verify:{uuid.uuid4().hex}"
        q, order = RedisQueue(client, RunGate(), key=key), []

        async def verify(name):
            job = await q.acquire(5.0)
            order.append(name)
            await asyncio.sleep(0.05)
            await q.release(job)

        try:
            tasks = []
            for name in ("a", "b", "c"):
                tasks.append(asyncio.create_task(verify(name)))
                await asyncio.sleep(0.02)
            await asyncio.gather(*tasks)
            return order, await client.llen(key)
        finally:
            await client.delete(key)
            await client.aclose()

    order, left = run(main())
    assert order == ["a", "b", "c"] and left == 0
