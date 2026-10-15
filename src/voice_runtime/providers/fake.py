"""确定性、可配置故障的假 Provider：核心测试只依赖它们。所有时间来自虚拟时钟。"""
import asyncio
import random
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from voice_runtime.audio import AudioChunk, Frame, WordTiming, rms, tone, word_duration_ms
from voice_runtime.providers.base import AsrEvent, AsrWord, TurnScore
from voice_runtime.script import Noise, Speech
from voice_runtime.text import PUNCT, normalize_text
from voice_runtime.vloop import now_ms

_END = object()
_WOKE = object()  # 进行中的等待被 cancel() 打断
CHUNK_MS = 200
ENERGY_RMS = 1000.0


async def _hang() -> None:
    await asyncio.Event().wait()


async def _sleep_until(due_ms: int) -> None:
    delay = due_ms - now_ms()
    if delay > 0:
        await asyncio.sleep(delay / 1000)


@dataclass(frozen=True)
class StreamFaults:
    first_delay_ms: int
    interval_ms: int
    jitter_ms: int = 20
    seed: int = 0
    hang_after: int | None = None
    raise_at: int | None = None
    late_after_cancel_ms: tuple[int, ...] = ()
    swallow_first_cancel: bool = False


class FakeVAD:
    def __init__(self, threshold_rms: float = 1000.0):
        self._threshold = threshold_rms

    def process(self, frame: Frame) -> float:
        return 0.9 if rms(frame.pcm) > self._threshold else 0.05


class _FakeStream:
    """故障注入骨架。子类实现 _produce（下一项或 _END）与 _late_item（cancel 之后的补发项）。"""

    def __init__(self, faults: StreamFaults):
        self._f = faults
        self._rng = random.Random(faults.seed)
        self._n = 0
        self._late_n = 0
        self._cancelled_at: int | None = None
        self._swallowed = False
        self._waiter: asyncio.Future | None = None

    def _jitter(self) -> int:
        return self._rng.randint(0, self._f.jitter_ms)

    def cancel(self) -> None:
        if self._cancelled_at is None:
            self._cancelled_at = now_ms()
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(False)  # 唤醒正在进行的等待：cancel 胜出

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
            try:
                item = await self._step()
                break
            except asyncio.CancelledError:
                if not self._f.swallow_first_cancel or self._swallowed:
                    raise
                # 测试专用：整个 src 里唯一允许吞掉取消的地方（第 1 次 CancelledError 吞掉，
                # 第 2 次才退出），用来模拟不配合取消的下游，验证 Task 14 的关闭重试。
                self._swallowed = True
                asyncio.current_task().uncancel()
        if item is _END:
            raise StopAsyncIteration
        return item

    async def _wait(self, delay_ms: int | None) -> bool:
        """等 delay_ms（None=永远）。正常结束返回 True；被 cancel() 唤醒返回 False。
        只用一个 future + call_later，不派生任务。"""
        if self._cancelled_at is not None:
            return False
        loop = asyncio.get_running_loop()
        fut = self._waiter = loop.create_future()
        handle = None
        if delay_ms is not None:
            handle = loop.call_later(max(delay_ms, 0) / 1000,
                                     lambda: fut.done() or fut.set_result(True))
        try:
            return await fut
        finally:
            if handle:
                handle.cancel()
            self._waiter = None

    async def _wait_until(self, due_ms: int) -> bool:
        return await self._wait(due_ms - now_ms())

    async def _step(self):
        while True:
            if self._cancelled_at is not None:
                if self._late_n >= len(self._f.late_after_cancel_ms):
                    return _END
                await _sleep_until(self._cancelled_at + self._f.late_after_cancel_ms[self._late_n])
                self._late_n += 1
                return self._late_item()
            if self._f.hang_after == self._n:
                await self._wait(None)
                continue
            if self._f.raise_at == self._n:
                raise RuntimeError("fake failure")
            item = await self._produce()
            if item is _WOKE:
                continue
            if item is not _END:
                self._n += 1
            return item

    async def _produce(self):
        raise NotImplementedError

    def _late_item(self):
        raise NotImplementedError


class _LlmStream(_FakeStream):
    def __init__(self, faults: StreamFaults, reply: str):
        super().__init__(faults)
        self._words = reply.split()
        self._idx = 0
        self._start = now_ms()
        self._last = self._start

    async def _produce(self):
        if self._idx >= len(self._words):
            return _END
        f = self._f
        due = (self._start + f.first_delay_ms if self._idx == 0
               else self._last + f.interval_ms) + self._jitter()
        if not await self._wait_until(due):
            return _WOKE
        self._last = now_ms()
        return self._take()

    def _take(self) -> str:
        w = self._words[self._idx]
        self._idx += 1
        return w + " "

    def _late_item(self) -> str:
        if self._idx < len(self._words):
            return self._take()
        return (self._words[-1] if self._words else "") + " "


class FakeLLM:
    def __init__(self, replies: dict[str, str], default_reply: str, faults: StreamFaults):
        self._replies = {normalize_text(k): v for k, v in replies.items()}
        self._default = default_reply
        self._faults = faults

    def start(self, messages: list[dict]) -> _LlmStream:
        user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
        return _LlmStream(self._faults, self._replies.get(normalize_text(user), self._default))


class _TtsStream(_FakeStream):
    def __init__(self, faults: StreamFaults, text_stream: AsyncIterator[str],
                 leading_noise_ms: int, amplitude: int):
        super().__init__(faults)
        self._src = text_stream
        self._lead = leading_noise_ms
        self._amp = amplitude
        self._text = ""
        self._scan = 0
        self._chunk_end = 0
        self._eos = False
        self._pending: list[tuple[str, int, int]] = []
        self._first_text_at: int | None = None
        self._last_emit = 0
        self._fetch_task: asyncio.Task | None = None
        self._started = False  # 已吐出第一个 chunk

    def _scan_words(self) -> None:
        base = self._scan
        for m in re.finditer(r"\S+", self._text[base:]):
            s, e = base + m.start(), base + m.end()
            if e == len(self._text) and not self._eos:
                return  # 末尾的词后面还没有空白，尚未完整
            self._pending.append((m.group(), s, e))
            self._scan = e

    async def _fetch(self):
        async def nxt():
            try:
                return await anext(self._src)
            except StopAsyncIteration:
                return _END

        if self._fetch_task is None:
            self._fetch_task = asyncio.create_task(nxt(), name="fake/tts-fetch")
        task = self._fetch_task
        fut = self._waiter = asyncio.get_running_loop().create_future()
        if task.done():
            fut.set_result(True)
        else:
            task.add_done_callback(lambda _: fut.done() or fut.set_result(True))
        try:
            woke = not await fut
        except asyncio.CancelledError:
            # 第一次（将被吞掉的）取消：保留在途的 fetch，重跑时继续等它；否则真正退出，回收
            if not (self._f.swallow_first_cancel and not self._swallowed):
                await self._reap(task)
            raise
        finally:
            self._waiter = None
        if woke:
            await self._reap(task)  # cancel() 胜出：在途的这段文本会丢
            return _WOKE
        self._fetch_task = None
        return task.result()

    async def _reap(self, task: asyncio.Task) -> None:
        """退出前取消并等待 fetch 任务，并取走它的异常，避免任务泄漏与未取回异常日志。"""
        self._fetch_task = None
        if not task.done():
            task.cancel()
        await asyncio.wait({task})
        if not task.cancelled():
            task.exception()

    async def _fill(self) -> bool:
        while not self._eos and sum(word_duration_ms(w) for w, _, _ in self._pending) < CHUNK_MS:
            piece = await self._fetch()
            if piece is _WOKE:
                return False
            if piece is _END:
                self._eos = True
            else:
                if self._first_text_at is None:
                    self._first_text_at = now_ms()
                self._text += piece
            self._scan_words()
        return True

    def _make_chunk(self, words: list[tuple[str, int, int]], end: int) -> AudioChunk:
        pcm, timings, t = b"", [], 0
        if not self._started and self._lead:
            pcm, t = tone(self._lead, 200), self._lead
        for w, _, _ in words:
            d = word_duration_ms(w)
            pcm += tone(d, self._amp)
            timings.append(WordTiming(w, t, t + d))
            t += d
        chunk = AudioChunk(pcm, (self._chunk_end, end), timings)
        self._chunk_end = end
        self._started = True
        return chunk

    async def _produce(self):
        if self._cancelled_at is not None or not await self._fill():
            return _WOKE
        if not self._pending:
            return _END
        base = (self._first_text_at + self._f.first_delay_ms if not self._started
                else self._last_emit + self._f.interval_ms)
        if not await self._wait_until(base + self._jitter()):
            return _WOKE  # pending 保留，作为 late 项的内容
        words, self._pending = self._pending, []
        self._last_emit = now_ms()
        return self._make_chunk(words, words[-1][2])

    def _late_item(self) -> AudioChunk:
        if self._pending:
            words, self._pending = self._pending, []
            return self._make_chunk(words, words[-1][2])
        n = len(self._text)
        chunk = AudioChunk(tone(CHUNK_MS, self._amp), (n, n))
        self._started = True
        return chunk


class FakeTTS:
    def __init__(self, faults: StreamFaults, leading_noise_ms: int = 0, amplitude: int = 3000):
        self._faults = faults
        self._lead = leading_noise_ms
        self._amp = amplitude

    def start(self, text_stream: AsyncIterator[str]) -> _TtsStream:
        return _TtsStream(self._faults, text_stream, self._lead, self._amp)


class FakeASR:
    def __init__(self, segments: list, *, partial_latency_ms: int = 100,
                 final_latency_ms: int = 150, jitter_ms: int = 20, seed: int = 1,
                 hang_at_ms: int | None = None):
        self._script = [s for s in segments if isinstance(s, (Speech, Noise))]
        self._partial = partial_latency_ms
        self._final = final_latency_ms
        self._jitter_ms = jitter_ms
        self._rng = random.Random(seed)
        self._hang_at = hang_at_ms

    def _jitter(self) -> int:
        return self._rng.randint(0, self._jitter_ms)

    def _partials(self, k: int, start: int) -> list[tuple[int, AsrEvent]]:
        seg, t, words, out = self._script[k], start, [], []
        for w in (seg.text.split() if isinstance(seg, Speech) else []):
            end = t + word_duration_ms(w)
            words.append(AsrWord(w, t, end))
            text = " ".join(x.w.translate(PUNCT) for x in words)
            out.append((end + self._partial + self._jitter(),
                        AsrEvent("partial", text, tuple(words), k, start, end)))
            t = end
        return out

    def _final_event(self, k: int, start: int, end: int, floor: int) -> tuple[int, AsrEvent]:
        seg, t, words = self._script[k], start, []
        for w in (seg.text.split() if isinstance(seg, Speech) else []):
            words.append(AsrWord(w, t, t + word_duration_ms(w)))
            t += word_duration_ms(w)
        text = seg.text if isinstance(seg, Speech) else ""
        return max(end + self._final + self._jitter(), floor), AsrEvent("final", text, tuple(words), k, start, end)

    async def run(self, frames: AsyncIterator[Frame]) -> AsyncIterator[AsrEvent]:
        due: list[tuple[int, AsrEvent]] = []
        k, seg_start, floor = -1, None, 0  # 已检测到的段落序号；当前段落起点（段外为 None）
        async for frame in frames:
            if self._hang_at is not None and frame.ts_ms >= self._hang_at:
                await _hang()
            loud = rms(frame.pcm) > ENERGY_RMS
            if loud and seg_start is None:
                k, seg_start = k + 1, frame.ts_ms
                parts = self._partials(k, seg_start)
                due += parts
                floor = max((d for d, _ in parts), default=0)
            elif not loud and seg_start is not None:
                due.append(self._final_event(k, seg_start, frame.ts_ms, floor))
                seg_start = None
            due.sort(key=lambda d: d[0])  # 稳定排序：同一到期时刻保持产生顺序
            while due and due[0][0] <= frame.ts_ms:
                yield due.pop(0)[1]


class ScriptedTurnScorer:
    def __init__(self, table: dict[str, TurnScore], default: TurnScore, delay_ms: int = 0,
                 hang: bool = False, raise_exc: bool = False):
        self._table = {normalize_text(k): v for k, v in table.items()}
        self._default = default
        self._delay_ms = delay_ms
        self._hang = hang
        self._raise = raise_exc

    async def score(self, text: str, audio: bytes | None = None) -> TurnScore:
        if self._delay_ms:
            await asyncio.sleep(self._delay_ms / 1000)
        if self._hang:
            await _hang()
        if self._raise:
            raise RuntimeError("fake failure")
        return self._table.get(normalize_text(text), self._default)


class FakeBackchannelScorer:
    def __init__(self, by_slice_count: Callable[[int], list[float]] | None = None,
                 default_score: float = 0.8, delay_ms: int = 0, fail: bool = False):
        self._by_count = by_slice_count
        self._default = default_score
        self._delay_ms = delay_ms
        self._fail = fail

    async def score(self, slices: list[bytes]) -> list[float]:
        if self._delay_ms:
            await asyncio.sleep(self._delay_ms / 1000)
        if self._fail:
            raise RuntimeError("fake failure")
        n = len(slices)
        return self._by_count(n) if self._by_count else [self._default] * n
