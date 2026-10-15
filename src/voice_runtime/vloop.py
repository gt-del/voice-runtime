"""虚拟时间 event loop：time() 返回虚拟时钟，空闲时直接跳到下一个定时器。"""
import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


class VirtualLoopStalled(RuntimeError):
    """没有就绪回调也没有定时器，loop 将永远空闲。"""


class _VirtualSelector:
    def __init__(self, real, loop: "VirtualTimeLoop"):
        self._real = real
        self._loop = loop

    def select(self, timeout=None):
        events = self._real.select(0)
        if events or timeout == 0:
            return events
        if timeout is None:
            raise VirtualLoopStalled("no ready callbacks and no timers")
        self._loop._vt += timeout
        return []

    def __getattr__(self, name):
        return getattr(self._real, name)


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    def __init__(self):
        super().__init__()
        self._vt = 0.0
        self._selector = _VirtualSelector(self._selector, self)

    def time(self) -> float:
        return self._vt


def now_ms() -> int:
    return round(asyncio.get_running_loop().time() * 1000)


def run(coro: Coroutine[Any, Any, T]) -> T:
    loop = VirtualTimeLoop()
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()
