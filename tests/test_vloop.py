import asyncio
import time

import pytest

from voice_runtime.vloop import VirtualLoopStalled, now_ms, run


def test_sleep_advances_virtual_time_instantly():
    async def main():
        t0 = now_ms(); await asyncio.sleep(3600); return now_ms() - t0
    wall = time.monotonic()
    assert run(main()) == 3_600_000
    assert time.monotonic() - wall < 1.0


def test_timeout_fires_in_virtual_time():
    async def main():
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.25):
                await asyncio.sleep(10)
        return now_ms()
    assert run(main()) == 250


def test_interleaving_is_deterministic():
    async def main():
        order = []
        async def w(name, d):
            for _ in range(3):
                await asyncio.sleep(d); order.append((name, now_ms()))
        async with asyncio.TaskGroup() as tg:
            tg.create_task(w("a", 0.02), name="a"); tg.create_task(w("b", 0.03), name="b")
        return order
    assert run(main()) == run(main())


def test_idle_without_timers_raises_instead_of_hanging():
    async def main():
        await asyncio.Event().wait()
    with pytest.raises(VirtualLoopStalled):
        run(main())
