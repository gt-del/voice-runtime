"""播放器：统一门控、20ms tick、闸③、EOS、欠载与 progress 区间（设计 §8.3、§11.2）。"""
import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from voice_runtime.audio import FRAME_MS, FRAME_SAMPLES
from voice_runtime.config import RuntimeConfig
from voice_runtime.events import (PLAYBACK_PROGRESS, PLAYBACK_RESUMED, PLAYBACK_STARTED,
                                  PLAYBACK_STOPPED, PLAYBACK_UNDERRUN, STALE_EVENT_DROPPED,
                                  EventLog)
from voice_runtime.queues import BoundedQueue
from voice_runtime.truth import TruthLedger
from voice_runtime.vloop import now_ms


@dataclass(frozen=True)
class PlaybackFrame:
    generation_id: int
    chunk_id: int
    frame_index: int
    pcm: bytes


@dataclass(frozen=True)
class PlaybackEOS:
    generation_id: int
    truncated: bool


class GateState(StrEnum):
    IDLE = "idle"
    HELD = "held"
    PLAYING = "playing"
    PAUSED = "paused"
    CLOSED = "closed"


class AudioSink(Protocol):
    def write(self, generation_id: int, frame_index: int, pcm: bytes) -> None: ...

    def close(self) -> None: ...


class FakeSink:
    def __init__(self) -> None:
        self.writes: list[tuple[int, int, int]] = []
        self.closed = False

    def write(self, generation_id: int, frame_index: int, pcm: bytes) -> None:
        assert not self.closed, "write after sink closed"
        self.writes.append((now_ms(), generation_id, frame_index))

    def close(self) -> None:
        self.closed = True


class Player:
    def __init__(self, cfg: RuntimeConfig, log: EventLog,
                 queue: BoundedQueue[PlaybackFrame | PlaybackEOS], sink: AudioSink,
                 is_active: Callable[[int], bool], ledgers: Callable[[int], TruthLedger],
                 turn_of: Callable[[int], int]):
        self._cfg = cfg
        self._log = log
        self._queue = queue
        self._sink = sink
        self._is_active = is_active
        self._ledgers = ledgers
        self._turn_of = turn_of
        self.state = GateState.IDLE
        self.current_generation: int | None = None
        self.on_generation_done: Callable[[int, str], None] = lambda gen, reason: None
        self._written = 0  # 当前 generation 已写入 sink 的帧数
        self._pending: list[list[int]] = []  # 尚未记入 progress 的帧区间 [gen, first, last]
        self._pending_since = 0  # 第一个未记录帧的写入时刻
        self._underrun = False
        self._paused_at = 0

    def _emit(self, event_type: str, gen: int, **payload) -> None:
        self._log.emit(event_type, turn_id=self._turn_of(gen), generation_id=gen, **payload)

    def hold(self, generation_id: int) -> None:
        if self.state is GateState.CLOSED:
            return
        self._flush_progress()  # 切换 generation 前补记旧区间，保证每帧写入都有 progress 记录
        self.state = GateState.HELD
        self.current_generation = generation_id
        self._written = 0
        self._pending = []
        self._underrun = False

    def release(self, generation_id: int) -> None:
        if self.state is GateState.HELD and generation_id == self.current_generation:
            self.state = GateState.PLAYING

    def unhold(self, generation_id: int) -> None:
        """放下一个还没出声的 held generation（pending 阶段被作废），不写 playback_stopped。"""
        if self.state is GateState.HELD and generation_id == self.current_generation:
            self.state = GateState.IDLE
            self.current_generation = None

    def pause(self) -> int:
        position = self._written * FRAME_SAMPLES
        if self.state is not GateState.PLAYING:
            return position
        gen = self.current_generation
        self._flush_progress()
        self.state = GateState.PAUSED
        self._paused_at = now_ms()
        self._emit(PLAYBACK_STOPPED, gen, reason="paused_pending_decision",
                   position_samples=position)
        return position

    def resume(self) -> None:
        if self.state is not GateState.PAUSED:
            return
        self.state = GateState.PLAYING
        self._emit(PLAYBACK_RESUMED, self.current_generation, paused_ms=now_ms() - self._paused_at)

    def stop_generation(self, generation_id: int, reason: str) -> None:
        is_current = generation_id == self.current_generation
        if is_current:
            self._flush_progress()
        self._emit(PLAYBACK_STOPPED, generation_id, reason=reason,
                   playback_truth=self._ledgers(generation_id).summary())
        if is_current and self.state is not GateState.CLOSED:
            self.state = GateState.IDLE
            self.current_generation = None

    def close(self) -> None:
        self.state = GateState.CLOSED
        self._sink.close()

    async def run(self) -> None:
        while self.state is not GateState.CLOSED:
            await asyncio.sleep(FRAME_MS / 1000)
            self._tick()

    def _tick(self) -> None:
        if self.state is not GateState.PLAYING:
            return
        # 先补记已满一个周期的区间，再取本 tick 的帧，使 progress 严格每 progress_interval_ms 一次
        if self._pending and now_ms() - self._pending_since >= self._cfg.progress_interval_ms:
            self._flush_progress()
        self._take_one()

    def _take_one(self) -> None:
        """取到一帧有效帧写入、处理完一个 EOS 或队列取空为止；过期项同一 tick 内连续丢弃。"""
        while True:
            item = self._queue.get_nowait()
            if item is None:
                # 欠载只在本 generation 已开播后才计
                if self._written and not self._underrun:
                    self._underrun = True
                    self._emit(PLAYBACK_UNDERRUN, self.current_generation)
                return
            gen = item.generation_id
            # 闸③：非当前或已失效的 generation 一律不写 sink
            stale = gen != self.current_generation or not self._is_active(gen)
            if isinstance(item, PlaybackEOS):
                if stale:
                    self._emit(STALE_EVENT_DROPPED, gen, gate="3", phase="late", unit="eos")
                    continue
                reason = "truncated" if item.truncated else "completed"
                self.stop_generation(gen, reason)
                self.on_generation_done(gen, reason)
                return
            if stale:
                self._emit(STALE_EVENT_DROPPED, gen, gate="3", phase="late", unit="frame",
                           frame_index=item.frame_index)
                continue
            self._write(item)
            return

    def _write(self, item: PlaybackFrame) -> None:
        gen = item.generation_id
        self._sink.write(gen, item.frame_index, item.pcm)
        self._ledgers(gen).on_played(item.chunk_id, item.frame_index)
        self._written += 1
        self._underrun = False
        if self._written == 1:
            self._emit(PLAYBACK_STARTED, gen)
        if not self._pending:
            self._pending_since = now_ms()
        if self._pending and self._pending[-1][2] + 1 == item.frame_index:
            self._pending[-1][2] = item.frame_index
        else:
            self._pending.append([gen, item.frame_index, item.frame_index])

    def _flush_progress(self) -> None:
        if not self._pending:
            return
        gen = self.current_generation
        self._emit(PLAYBACK_PROGRESS, gen, ranges=self._pending,
                   played_ms=self._written * FRAME_MS)
        self._pending = []
