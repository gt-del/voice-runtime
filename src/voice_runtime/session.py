"""Session：唯一的 owner / 协调者——ID、状态机、TaskGroup、active_generation_id（设计 §4、§8、§10）。"""
import asyncio
from collections import deque
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from voice_runtime.audio import FRAME_MS, Frame
from voice_runtime.config import RuntimeConfig
from voice_runtime.endpointing.cascade import EndpointCascade, EndpointDecision
from voice_runtime.events import (ASR_FINAL, ASR_PARTIAL, AUDIO_FRAME, CONTEXT_COMMITTED,
                                  ENDPOINT_COMMITTED, GENERATION_CANCELLED, GENERATION_FAILED,
                                  PLAYBACK_STARTED, QUEUE_OVERFLOW, SESSION_CLOSED, SPEECH_END,
                                  SPEECH_START, STALE_EVENT_DROPPED, EventLog)
from voice_runtime.generation import Generation, GenerationSupervisor, GenState
from voice_runtime.playback import AudioSink, Player
from voice_runtime.providers.base import ASR, LLM, TTS, VAD, BackchannelScorer, TurnScorer
from voice_runtime.queues import BoundedQueue, QueueClosed
from voice_runtime.script import ScriptedAudioSource
from voice_runtime.speech import SpeechEdge, SpeechTracker
from voice_runtime.truth import TruthLedger
from voice_runtime.vloop import now_ms


@dataclass
class Providers:
    vad: VAD
    asr: ASR
    llm: LLM
    tts: TTS
    l1: TurnScorer
    backchannel: BackchannelScorer
    l2: TurnScorer | None = None
    l3: TurnScorer | None = None


class CloseReason(StrEnum):
    EXPLICIT_CLOSE = "explicit_close"
    CLIENT_DISCONNECT = "client_disconnect"
    SESSION_TASK_FAILED = "session_task_failed"


class SessionState(StrEnum):
    LISTENING = "listening"
    PENDING = "pending"
    PLAYING = "playing"
    PAUSED = "paused"
    CLOSING = "closing"
    CLOSED = "closed"


class Session:
    def __init__(self, cfg: RuntimeConfig, providers: Providers, source: ScriptedAudioSource,
                 sink: AudioSink, log: EventLog):
        self.cfg = cfg
        self._p = providers
        self._source = source
        self._log = log
        self.state = SessionState.LISTENING
        self.queues: dict[str, BoundedQueue] = {
            "asr_in": BoundedQueue("asr_in", cfg.asr_in_capacity, overflow="drop_oldest"),
            "playback": BoundedQueue("playback", cfg.playback_capacity_frames),
        }
        self.context: deque[dict] = deque(maxlen=cfg.context_max_messages)
        self.active_generation_id: int | None = None
        self.turn_id = 0
        self.generation_id = 0
        self._turn_open = False
        self._turn_text = ""
        self._pending_decision: EndpointDecision | None = None  # 已判停、等 final 中
        self._gens: dict[int, Generation] = {}
        self._sups: dict[int, GenerationSupervisor] = {}
        self._running_sups: set[int] = set()  # supervisor 任务尚未退出的 generation
        self._tracker = SpeechTracker(cfg)
        self._cascade = EndpointCascade(cfg, log, [providers.l1], spawn=self._spawn)
        self._player = Player(cfg, log, self.queues["playback"], sink,
                              is_active=lambda gid: gid == self.active_generation_id,
                              ledgers=self._ledger_of,
                              turn_of=self._turn_of)
        self._player.on_generation_done = self._on_generation_done
        self._tg: asyncio.TaskGroup | None = None
        self._tasks: set[asyncio.Task] = set()
        self._stop_event = asyncio.Event()
        self._close_reason: CloseReason | None = None
        self._shut_down = False
        log.add_listener(self._on_log_event)

    # ---- 公共接口 ----

    def close(self, reason: CloseReason = CloseReason.EXPLICIT_CLOSE) -> None:
        if self._close_reason is None:
            self._close_reason = reason
        self._stop_event.set()

    async def run(self) -> CloseReason:
        error = None
        try:
            async with asyncio.TaskGroup() as tg:
                self._tg = tg
                self._spawn(self._audio_pump(), "session/audio_pump")
                self._spawn(self._asr_consumer(), "session/asr_consumer")
                self._spawn(self._control(), "session/control")
                self._spawn(self._player.run(), "session/player")
                await self._stop_event.wait()
                self._shutdown()
        except* Exception as eg:
            e = eg.exceptions[0]
            error = f"{type(e).__name__}: {e}"
            self._close_reason = CloseReason.SESSION_TASK_FAILED
            self._shutdown()
        finally:
            self._tg = None
        self.state = SessionState.CLOSED
        reason = self._close_reason
        self._log.emit(SESSION_CLOSED, turn_id=self._tid, reason=str(reason),
                       **({"error": error} if error else {}))
        return reason

    def invalidate(self, gen: Generation, reason: str) -> None:
        """设计 §10.1 第 1、2 步 + 发取消信号；同步执行，中间没有 await。按 generation 幂等。"""
        if gen.state is GenState.CANCELLED:
            self._log.emit(GENERATION_CANCELLED, turn_id=gen.turn_id, generation_id=gen.id,
                           reason=reason, duplicate=True)
            return
        if gen.id != self.active_generation_id:
            return
        self.active_generation_id = None
        gen.state = GenState.CANCELLED
        self._log.emit(GENERATION_CANCELLED, turn_id=gen.turn_id, generation_id=gen.id,
                       reason=reason, duplicate=False)
        n = self.queues["playback"].purge(lambda it: it.generation_id == gen.id)
        self._log.emit(STALE_EVENT_DROPPED, turn_id=gen.turn_id, generation_id=gen.id,
                       gate="purge", phase="purge", unit="frame", frames=n)
        gen.text_queue.close()
        self._sups[gen.id].signal_cancel()

    # ---- 任务与关闭 ----

    @property
    def _tid(self) -> int | None:
        return self.turn_id or None

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task:
        task = self._tg.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _shutdown(self) -> None:
        """最简关闭（Task 14 替换为完整的关闭序列）：作废在播 generation、关播放器、取消所有任务。"""
        if self._shut_down:
            return
        self._shut_down = True
        self.state = SessionState.CLOSING
        gid = self.active_generation_id
        if gid is not None:
            if self._player.current_generation == gid:
                self._player.stop_generation(gid, "session_closed")
            self.invalidate(self._gens[gid], "session_closed")
        self._player.close()
        for t in list(self._tasks):
            t.cancel()

    # ---- session 级循环 ----

    async def _audio_pump(self) -> None:
        asr_in = self.queues["asr_in"]
        async for frame in self._source.frames():
            if self.cfg.log_audio_frames:
                self._log.emit(AUDIO_FRAME, turn_id=self._tid, seq=frame.seq, ts_ms=frame.ts_ms)
            edge = self._tracker.feed(frame, self._p.vad.process(frame))
            if edge is not None:
                self._on_speech_edge(edge)
            before = asr_in.dropped_count
            await asr_in.put(frame)
            if asr_in.dropped_count > before:
                self._log.emit(QUEUE_OVERFLOW, turn_id=self._tid, queue="asr_in",
                               depth=asr_in.depth, dropped=asr_in.dropped_count - before,
                               dropped_total=asr_in.dropped_count)
        if self._source.disconnected:
            self.close(CloseReason.CLIENT_DISCONNECT)

    async def _asr_frames(self) -> AsyncIterator[Frame]:
        q = self.queues["asr_in"]
        while True:
            try:
                yield await q.get()
            except QueueClosed:
                return

    async def _asr_consumer(self) -> None:
        async for ev in self._p.asr.run(self._asr_frames()):
            self._log.emit(ASR_FINAL if ev.kind == "final" else ASR_PARTIAL, turn_id=self._tid,
                           text=ev.text, segment_id=ev.segment_id,
                           segment_start_ms=ev.segment_start_ms, segment_end_ms=ev.segment_end_ms)
            # 只有 LISTENING 且轮次已开启时文本才进 cascade；播放期间的语音归打断门禁（Task 13）
            if self.state is SessionState.LISTENING and self._turn_open:
                self._cascade.on_text(ev.text, ev.kind == "final")

    async def _control(self) -> None:
        while True:
            await asyncio.sleep(FRAME_MS / 1000)
            self._control_tick(now_ms())

    # ---- 判停与轮次 ----

    def _on_speech_edge(self, edge: SpeechEdge) -> None:
        listening = self.state is SessionState.LISTENING
        if edge.kind == "start" and listening and not self._turn_open:
            self._open_turn()
        self._log.emit(SPEECH_START if edge.kind == "start" else SPEECH_END, turn_id=self._tid,
                       ts_ms=edge.ts_ms, epoch=edge.epoch)
        # PENDING / PLAYING 中的语音边界由打断门禁处理（Task 13），这里不参与判停
        if listening:
            if edge.kind == "start":
                self._pending_decision = None  # 等 final 期间用户又开口：判停作废，cascade 重新判
            self._cascade.on_speech_edge(edge)

    def _open_turn(self) -> None:
        self.turn_id += 1
        self._turn_open = True
        self._cascade.turn_id = self.turn_id

    def _control_tick(self, now: int) -> None:
        if self.state is not SessionState.LISTENING or not self._turn_open:
            return
        if self._pending_decision is None:
            self._pending_decision = self._cascade.tick(now)
            if self._pending_decision is None:
                return
        d = self._pending_decision
        if self._cascade.have_final_for_last_segment:
            source = "final"
        elif now - d.decided_at_ms >= self.cfg.final_wait_ms:
            source = "partial"
        else:
            return
        self._pending_decision = None
        self._turn_text = self._cascade.current_text
        self._log.emit(ENDPOINT_COMMITTED, turn_id=self.turn_id, decided_at=d.decided_at_ms,
                       text_source=source, reason=d.reason, level=d.level, p=d.p,
                       threshold=d.threshold, text=self._turn_text)
        self._start_generation(self._turn_text)

    def _start_generation(self, text: str) -> None:
        self.generation_id += 1
        gid = self.generation_id
        gen = Generation(id=gid, turn_id=self.turn_id, user_text=text, speculative=False,
                         state=GenState.ACTIVE,
                         text_queue=BoundedQueue(f"text-gen{gid}", self.cfg.text_capacity),
                         ledger=TruthLedger(gid))
        self._gens[gid] = gen
        self.active_generation_id = gid
        messages = [*self.context, {"role": "user", "content": text}]
        sup = GenerationSupervisor(gen, self.cfg, self._log, self._p.llm, self._p.tts, messages,
                                   self.queues["playback"],
                                   is_active=lambda g: g.id == self.active_generation_id,
                                   on_failed=self._on_failed)
        self._sups[gid] = sup
        self._running_sups.add(gid)
        task = self._spawn(sup.run(), f"gen-{gid}/supervisor")
        task.add_done_callback(lambda _: self._on_supervisor_exit(gid))
        self._player.hold(gid)
        self._player.release(gid)
        self.state = SessionState.PENDING

    def _on_supervisor_exit(self, gid: int) -> None:
        self._running_sups.discard(gid)
        self._prune(gid)

    def _prune(self, gid: int) -> None:
        """generation 不再 active、播放器不再持有、supervisor 已退出时，从注册表中移除。"""
        if (gid == self.active_generation_id or gid == self._player.current_generation
                or gid in self._running_sups):
            return
        self._gens.pop(gid, None)
        self._sups.pop(gid, None)

    def _turn_of(self, gid: int) -> int | None:
        gen = self._gens.get(gid)
        return gen.turn_id if gen is not None else None

    def _ledger_of(self, gid: int) -> TruthLedger:
        gen = self._gens.get(gid)
        return gen.ledger if gen is not None else TruthLedger(gid)  # 已清理：给空账本，不抛异常

    def _on_log_event(self, e: dict) -> None:
        if (e["event_type"] == PLAYBACK_STARTED and self.state is SessionState.PENDING
                and e["generation_id"] == self.active_generation_id):
            self.state = SessionState.PLAYING

    def _on_generation_done(self, gid: int, reason: str) -> None:
        gen = self._gens[gid]
        if gid == self.active_generation_id:
            self.active_generation_id = None
        gen.state = GenState.DONE
        self._end_turn(reason, gen)

    def _on_failed(self, gen: Generation, reason: str) -> None:
        if gen.id != self.active_generation_id:  # 已作废或已结束：只留幂等记录
            self.invalidate(gen, reason)
            return
        if self._player.current_generation == gen.id:
            self._player.stop_generation(gen.id, "generation_failed")
        self.invalidate(gen, reason)
        self._log.emit(GENERATION_FAILED, turn_id=gen.turn_id, generation_id=gen.id, reason=reason)
        self._end_turn("failed", gen)

    def _end_turn(self, outcome: str, gen: Generation) -> None:
        interrupted = outcome == "interrupted"
        heard = gen.ledger.summary()["heard_text"]
        self._log.emit(CONTEXT_COMMITTED, turn_id=gen.turn_id, generation_id=gen.id,
                       role="user", text=self._turn_text, outcome=outcome)
        self.context.append({"role": "user", "content": self._turn_text})
        if heard:
            self._log.emit(CONTEXT_COMMITTED, turn_id=gen.turn_id, generation_id=gen.id,
                           role="assistant", text=heard, interrupted=interrupted)
            self.context.append({"role": "assistant",
                                 "content": heard + (" (interrupted)" if interrupted else "")})
        self._turn_open = False
        self._turn_text = ""
        self._pending_decision = None
        self._cascade.reset()
        self.state = SessionState.LISTENING
        self._prune(gen.id)
        if self._tracker.in_speech:
            # 用户正说到一半（播放期间开口）：这段话直接成为下一轮，后续的累计 ASR 文本归入新轮次
            self._open_turn()
            self._cascade.on_speech_edge(
                SpeechEdge("start", self._tracker.speech_start_ms, self._tracker.epoch))
