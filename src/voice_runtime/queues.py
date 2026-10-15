"""有界队列：背压、丢最旧、醒来后重校验 accept、purge。"""

import asyncio
from collections import deque
from collections.abc import Callable
from typing import Generic, Literal, TypeVar

T = TypeVar("T")


class QueueClosed(Exception):
    pass


class BoundedQueue(Generic[T]):
    def __init__(
        self,
        name: str,
        capacity: int,
        *,
        overflow: Literal["block", "drop_oldest"] = "block",
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.name = name
        self.capacity = capacity
        self.max_depth = 0
        self.dropped_count = 0
        self._overflow = overflow
        self._items: deque[T] = deque()
        self._closed = False
        self._putters: list[asyncio.Future[None]] = []
        self._getters: list[asyncio.Future[None]] = []

    @property
    def depth(self) -> int:
        return len(self._items)

    @staticmethod
    def _wake(waiters: list["asyncio.Future[None]"]) -> None:
        for fut in waiters:
            if not fut.done():
                fut.set_result(None)
        waiters.clear()

    @staticmethod
    async def _wait(waiters: list["asyncio.Future[None]"]) -> None:
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        waiters.append(fut)
        try:
            await fut
        finally:
            if fut in waiters:
                waiters.remove(fut)

    async def put(self, item: T, accept: Callable[[T], bool] = lambda _: True) -> bool:
        if self._overflow == "block":
            while len(self._items) >= self.capacity and not self._closed:
                await self._wait(self._putters)
        # 醒来后的重校验与写入之间没有 await：同一个同步步骤
        if self._closed or not accept(item):
            return False
        if len(self._items) >= self.capacity:
            self._items.popleft()
            self.dropped_count += 1
        self._items.append(item)
        self.max_depth = max(self.max_depth, len(self._items))
        self._wake(self._getters)
        return True

    async def get(self) -> T:
        while not self._items:
            if self._closed:
                raise QueueClosed(self.name)
            await self._wait(self._getters)
        item = self._items.popleft()
        self._wake(self._putters)
        return item

    def get_nowait(self) -> T | None:
        if not self._items:
            return None
        item = self._items.popleft()
        self._wake(self._putters)
        return item

    def purge(self, pred: Callable[[T], bool]) -> int:
        kept = [x for x in self._items if not pred(x)]
        removed = len(self._items) - len(kept)
        self._items = deque(kept)
        if removed:
            self._wake(self._putters)
        return removed

    def close(self) -> int:
        self._closed = True
        residual = len(self._items)
        self._items.clear()
        self._wake(self._putters)
        self._wake(self._getters)
        return residual
