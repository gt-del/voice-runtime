"""Generation 与 GenerationSupervisor：LLM→TTS→playback 桥接、闸①②、取消排空、超时与 finish（设计 §5、§10）。"""
import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from enum import StrEnum

from voice_runtime.audio import FRAME_SAMPLES
from voice_runtime.config import RuntimeConfig
from voice_runtime.events import (AUDIO_ENQUEUED, CANCEL_GRACE_EXCEEDED, LLM_CHUNK,
                                  PROVIDER_TIMEOUT, STALE_EVENT_DROPPED, TASK_FAILED, TTS_CHUNK,
                                  EventLog)
from voice_runtime.playback import PlaybackEOS, PlaybackFrame
from voice_runtime.providers.base import LLM, TTS, ProviderStream
from voice_runtime.queues import BoundedQueue, QueueClosed
from voice_runtime.trim import trim_leading_noise
from voice_runtime.truth import TruthLedger
from voice_runtime.vloop import now_ms

FRAME_BYTES = FRAME_SAMPLES * 2
_TIMED_OUT = object()  # 我们自己的截止时间到期（区别于 provider 自己抛出的 TimeoutError）


class GenState(StrEnum):
    ACTIVE = "active"
    CANCELLED = "cancelled"
    UPSTREAM_CLOSED = "upstream_closed"
    DONE = "done"


@dataclass
class Generation:
    id: int
    turn_id: int
    user_text: str
    speculative: bool
    state: GenState
    text_queue: BoundedQueue[str | None]  # None 是 EOS；由 Session 创建，invalidate 时由 Session 关闭
    ledger: TruthLedger
    cancelled_at_ms: int | None = None


class GenerationSupervisor:
    def __init__(self, gen: Generation, cfg: RuntimeConfig, log: EventLog, llm: LLM, tts: TTS,
                 messages: list[dict], playback_queue: BoundedQueue,
                 is_active: Callable[[Generation], bool],
                 on_failed: Callable[[Generation, str], None],
                 is_closed: Callable[[], bool] = lambda: False):
        self.gen = gen
        self._cfg = cfg
        self._log = log
        self._llm = llm
        self._tts = tts
        self._messages = messages
        self._playback = playback_queue
        self._is_active = is_active
        self._on_failed = on_failed
        self._is_closed = is_closed  # session 已开始关闭：闸①②拒绝时记 gate=session_closed
        self._streams: list[ProviderStream] = []
        self._waits: dict[str, asyncio.Timeout] = {}  # 正在等上游下一项的超时上下文
        self._tg: asyncio.TaskGroup | None = None
        self._bridges: list[asyncio.Task] = []
        self._bridges_done = False
        self._grace: asyncio.Task | None = None
        self._failures: list[tuple[str, Exception]] = []
        self._eos_sent = False
        # TTS 计时状态（§5：首包从第一段文本送入起算；之后 TTS 等文本的时间不计入）
        self._tts_text_at: float | None = None
        self._tts_got_first = False
        self._tts_starved = False

    # ---- 公共接口 ----

    async def run(self) -> None:
        name = f"gen-{self.gen.id}"
        try:
            async with asyncio.TaskGroup() as tg:
                self._tg = tg
                self._bridges = [
                    tg.create_task(self._guard(f"{name}/llm_bridge", self._llm_bridge()),
                                   name=f"{name}/llm_bridge"),
                    tg.create_task(self._guard(f"{name}/tts_bridge", self._tts_bridge()),
                                   name=f"{name}/tts_bridge"),
                ]
                if self.gen.cancelled_at_ms is not None:  # run 之前就已收到取消信号
                    self._start_grace()
                await asyncio.wait(self._bridges)
                self._bridges_done = True
                if self._grace is not None:
                    self._grace.cancel()
        except* Exception:
            for task_name, e in self._failures:
                self._emit(TASK_FAILED, task=task_name, error=f"{type(e).__name__}: {e}")
            self._on_failed(self.gen, "task_failed")
        finally:
            self._tg = None
        if self._failures:
            return
        # 看门狗取消了 tts_bridge 时它没来得及写 EOS；finish 的 generation 仍需要 EOS 收尾
        if self.gen.state is GenState.UPSTREAM_CLOSED and not self._eos_sent:
            await self._put_eos()

    def signal_cancel(self) -> None:
        if self.gen.cancelled_at_ms is not None:
            return
        self.gen.cancelled_at_ms = now_ms()
        for cm in self._waits.values():  # 取消之后只排空，不再计 provider 超时（由 grace 兜底）
            if not cm.expired():
                cm.reschedule(None)
        for s in self._streams:
            s.cancel()
        self._start_grace()

    def pending_tasks(self) -> list[asyncio.Task]:
        """本 generation 仍未退出的子任务（bridge、grace），供 session 关闭时直接再取消。"""
        return [t for t in (*self._bridges, self._grace) if t is not None and not t.done()]

    def finish(self) -> None:
        self.gen.state = GenState.UPSTREAM_CLOSED
        self.gen.text_queue.close()  # 闸①此后一律拒绝；关闭让 TTS 的输入流正常结束
        self.signal_cancel()

    # ---- 内部 ----

    def _emit(self, event_type: str, **payload) -> None:
        self._log.emit(event_type, turn_id=self.gen.turn_id, generation_id=self.gen.id, **payload)

    def _accept(self, _item) -> bool:
        return (not self._is_closed() and self._is_active(self.gen)
                and self.gen.state is GenState.ACTIVE)

    def _received_live(self) -> bool:
        """收到上游项时 generation 是否还没被取消/终止：决定被拒时记 rejected_on_wake 还是 late。"""
        return self.gen.state is GenState.ACTIVE and self.gen.cancelled_at_ms is None

    def _reject(self, gate: str, received_live: bool, **payload) -> tuple[str, str]:
        if self._is_closed():
            gate = "session_closed"
        elif self.gen.state is GenState.UPSTREAM_CLOSED:
            gate = "finished"
        phase = "rejected_on_wake" if received_live else "late"
        self._emit(STALE_EVENT_DROPPED, gate=gate, phase=phase, **payload)
        return gate, phase

    async def _guard(self, name: str, coro) -> None:
        try:
            await coro
        except Exception as e:
            self._failures.append((name, e))
            raise

    def _start_grace(self) -> None:
        if self._tg is None or self._bridges_done or self._failures or self._grace is not None:
            return
        coro = self._watch_grace()
        try:
            self._grace = self._tg.create_task(coro, name=f"gen-{self.gen.id}/grace")
        except RuntimeError:  # TaskGroup 正在关闭：子任务都已被取消，不需要看门狗
            coro.close()

    async def _watch_grace(self) -> None:
        delay_ms = self.gen.cancelled_at_ms + self._cfg.cancel_grace_ms - now_ms()
        await asyncio.sleep(max(delay_ms, 0) / 1000)
        running = [t for t in self._bridges if not t.done()]
        if running:
            self._emit(CANCEL_GRACE_EXCEEDED, tasks=[t.get_name() for t in running])
            for t in running:
                t.cancel()

    def _open(self, stream: ProviderStream) -> ProviderStream:
        self._streams.append(stream)
        if self.gen.cancelled_at_ms is not None:
            stream.cancel()
        return stream

    async def _next(self, role: str, stream: ProviderStream, when: float | None):
        """只给"等上游下一项"计时；流结束返回 None，截止时间到期返回 _TIMED_OUT。
        provider 自己抛出的 TimeoutError 照常向上抛，按任务异常处理。"""
        if self.gen.cancelled_at_ms is not None:
            when = None
        cm = asyncio.timeout_at(when)
        try:
            async with cm:
                self._waits[role] = cm
                try:
                    return await anext(stream)
                finally:
                    del self._waits[role]
        except StopAsyncIteration:
            return None
        except TimeoutError:
            if cm.expired():
                return _TIMED_OUT
            raise

    def _on_timeout(self, provider: str, first: bool) -> None:
        if self.gen.cancelled_at_ms is not None:  # 超时与取消同时发生时，以取消为准
            return
        if first:
            self._emit(PROVIDER_TIMEOUT, provider=provider, first=True)
            self._on_failed(self.gen, "first_chunk_timeout")
            self.signal_cancel()
        else:
            # finish 会关闭 text 队列，TTS 尚未读走的文本随之丢弃，数量记在这里
            self._emit(PROVIDER_TIMEOUT, provider=provider, first=False,
                       discarded_text_pieces=self.gen.text_queue.depth)
            self.finish()

    async def _llm_bridge(self) -> None:
        loop = asyncio.get_running_loop()
        stream = self._open(self._llm.start(self._messages))
        when = loop.time() + self._cfg.llm_first_timeout_ms / 1000
        first = True
        while True:
            text = await self._next("llm", stream, when)
            if text is _TIMED_OUT:
                self._on_timeout("llm", first)
                continue
            if text is None:
                break
            first = False
            live = self._received_live()
            self._emit(LLM_CHUNK, text=text)
            self.gen.ledger.on_generated(text)
            # 闸①：先快速拒绝，再由 put 在醒来后的同一同步步骤内重校验
            if not (self._accept(text) and await self.gen.text_queue.put(text, self._accept)):
                self._reject("1", live, unit="text")
            when = loop.time() + self._cfg.inter_chunk_timeout_ms / 1000
        await self.gen.text_queue.put(None)  # EOS；队列已关闭时直接返回 False

    async def _text_iter(self) -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        while True:
            self._tts_starved = True
            if self._tts_got_first:
                self._reschedule_tts(None)  # 首包之后 TTS 在等文本：不算 TTS 慢
            try:
                piece = await self.gen.text_queue.get()
            except QueueClosed:
                piece = None
            finally:
                self._tts_starved = False
            if self._tts_text_at is None:
                self._tts_text_at = loop.time()
            # 交出文本、或文本已全部交出（EOS）时重新计时：TTS 剩下的冲刷也不能无限等
            self._reschedule_tts(self._tts_deadline())
            if piece is None:
                return
            yield piece

    def _tts_deadline(self) -> float | None:
        if self._tts_text_at is None:
            return None
        if not self._tts_got_first:
            return self._tts_text_at + self._cfg.tts_first_timeout_ms / 1000
        if self._tts_starved:
            return None
        return asyncio.get_running_loop().time() + self._cfg.inter_chunk_timeout_ms / 1000

    def _reschedule_tts(self, when: float | None) -> None:
        cm = self._waits.get("tts")
        if cm is not None and not cm.expired() and self.gen.cancelled_at_ms is None:
            cm.reschedule(when)

    async def _tts_bridge(self) -> None:
        stream = self._open(self._tts.start(self._text_iter()))
        chunk_id = 0
        frame_index = 0
        while True:
            chunk = await self._next("tts", stream, self._tts_deadline())
            if chunk is _TIMED_OUT:
                self._on_timeout("tts", not self._tts_got_first)
                continue
            if chunk is None:
                break
            if chunk_id == 0 and self._cfg.trim_enabled:  # 只裁每个 generation 的第一个 chunk
                chunk = trim_leading_noise(chunk, max_trim_ms=self._cfg.max_trim_ms)
            self._tts_got_first = True
            live = self._received_live()
            samples = len(chunk.pcm) // 2
            self._emit(TTS_CHUNK, chunk_id=chunk_id, text_range=list(chunk.text_range),
                       samples=samples, trimmed_samples=chunk.trimmed_samples)
            self.gen.ledger.on_synthesized(chunk_id, chunk)  # 必须先于任何一帧入队
            accepted = 0
            rejected = False
            for off in range(0, len(chunk.pcm), FRAME_BYTES):
                pcm = chunk.pcm[off:off + FRAME_BYTES].ljust(FRAME_BYTES, b"\x00")
                frame = PlaybackFrame(self.gen.id, chunk_id, frame_index, pcm)
                # 闸②：逐帧；一帧被拒，整个 chunk 剩余部分都不再入队
                if not (self._accept(frame) and await self._playback.put(frame, self._accept)):
                    rejected = True
                    break
                accepted += 1
                frame_index += 1
            if accepted:
                self.gen.ledger.on_enqueued(chunk_id, accepted)
            if rejected:
                gate, phase = self._reject("2", live, unit="chunk", chunk_id=chunk_id)
                self.gen.ledger.on_dropped(chunk_id, phase, gate)
            else:
                self._emit(AUDIO_ENQUEUED, chunk_id=chunk_id, frames=accepted, queue="playback",
                           depth=self._playback.depth)
            chunk_id += 1
        await self._put_eos()

    def _accept_eos(self, _item) -> bool:
        # EOS 过闸②只看 is_active 与 session 未关闭：finish 之后的 EOS 仍需放行
        return not self._is_closed() and self._is_active(self.gen)

    async def _put_eos(self) -> None:
        self._eos_sent = True
        live = self._received_live()
        eos = PlaybackEOS(self.gen.id, truncated=self.gen.state is GenState.UPSTREAM_CLOSED)
        if not (self._accept_eos(eos) and await self._playback.put(eos, self._accept_eos)):
            # EOS 不受 UPSTREAM_CLOSED 拦截：被拒只可能是 session 已关闭或 is_active 为假
            self._emit(STALE_EVENT_DROPPED, gate="session_closed" if self._is_closed() else "2",
                       phase="rejected_on_wake" if live else "late", unit="eos")
