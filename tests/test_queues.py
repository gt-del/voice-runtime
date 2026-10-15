import asyncio

import pytest

from voice_runtime.queues import BoundedQueue, QueueClosed
from voice_runtime.vloop import run


def test_put_blocks_when_full_until_get():
    async def main():
        q = BoundedQueue("p", 2)
        await q.put(1)
        await q.put(2)
        t = asyncio.create_task(q.put(3), name="t")
        await asyncio.sleep(0)
        assert not t.done()
        assert await q.get() == 1
        assert await t is True
        return q.depth, q.max_depth

    assert run(main()) == (2, 2)


def test_accept_rechecked_after_wake():
    async def main():
        q = BoundedQueue("p", 1)
        await q.put("old")
        valid = {"gen": 1}
        t = asyncio.create_task(q.put("x", accept=lambda _: valid["gen"] == 1), name="t")
        await asyncio.sleep(0)
        valid["gen"] = 2
        await q.get()
        return await t, q.depth

    assert run(main()) == (False, 0)


def test_purge_removes_matching_and_wakes_putters():
    async def main():
        q = BoundedQueue("p", 2)
        await q.put(1)
        await q.put(2)
        t = asyncio.create_task(q.put(3), name="t")
        await asyncio.sleep(0)
        n = q.purge(lambda x: x < 3)
        return n, await t, q.depth

    assert run(main()) == (2, True, 1)


def test_drop_oldest_never_blocks_and_counts():
    async def main():
        q = BoundedQueue("p", 3, overflow="drop_oldest")
        for i in range(5):
            assert await q.put(i) is True
        return q.depth, q.dropped_count, [await q.get() for _ in range(3)]

    assert run(main()) == (3, 2, [2, 3, 4])


def test_close_returns_residual_and_get_raises():
    async def main():
        q = BoundedQueue("p", 2)
        await q.put(1)
        await q.put(2)
        n = q.close()
        with pytest.raises(QueueClosed):
            await q.get()
        return n, await q.put(3)

    assert run(main()) == (2, False)


def test_close_wakes_blocked_getter_and_putter():
    async def main():
        g = BoundedQueue("g", 1)
        tg = asyncio.create_task(g.get(), name="tg")
        p = BoundedQueue("p", 1)
        await p.put(1)
        tp = asyncio.create_task(p.put(2), name="tp")
        await asyncio.sleep(0)
        g.close()
        p.close()
        with pytest.raises(QueueClosed):
            await tg
        return await tp

    assert run(main()) is False


def test_capacity_must_be_positive():
    with pytest.raises(ValueError):
        BoundedQueue("x", 0)


def test_get_nowait_returns_none_when_empty_and_wakes_putters():
    async def main():
        q = BoundedQueue("p", 1)
        assert q.get_nowait() is None
        await q.put(1)
        t = asyncio.create_task(q.put(2), name="t")
        await asyncio.sleep(0)
        assert not t.done()
        got = q.get_nowait()
        put_ok = await t
        q.close()
        return got, put_ok, q.get_nowait()

    assert run(main()) == (1, True, None)
