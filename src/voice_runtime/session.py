"""Session：唯一的 owner / 协调者——ID、状态机、TaskGroup、active_generation_id（设计 §4、§8、§10）。"""
import asyncio
from collections import deque
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from voice_runtime.audio import FRAME_MS, Frame
from voice_runtime.config import RuntimeConfig
from voice_runtime.endpointing.cascade import EndpointCascade, EndpointDecision
from voice_runtime.events import (ASR_FINAL, ASR_PARTIAL, AUDIO_FRAME, BARGEIN_DECISION, CLOSE_TIMEOUT,
                                  CONTEXT_COMMITTED, ENDPOINT_COMMITTED, GENERATION_CANCELLED, GENERATION_FAILED,
                                  PLAYBACK_STARTED, QUEUE_OVERFLOW, SESSION_CLOSED, SPEECH_END,
                                  SPEECH_START, STALE_EVENT_DROPPED, EventLog)
from voice_runtime.gate import GateResult, Utterance, decide
from voice_runtime.generation import Generation, GenerationSupervisor, GenState
from voice_runtime.playback import AudioSink, Player
from voice_runtime.providers.base import (ASR, LLM, TTS, VAD, AsrEvent, BackchannelScorer,
                                          TurnScorer)
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


@dataclass(eq=False)  # 按身份比较：deque 的 in / remove 不能把两段话当成同一段
class _GateUtterance:
    """门禁正在评估（或最近判定过）的一段话：Utterance 加上 Session 侧的路由与判定状态。"""
    u: Utterance
    epoch: int  # 这段话的 silence epoch，交给 cascade 时沿用
    generation_id: int  # 开口时在播（或 pending）的 generation
    segments: dict[int, tuple[str, bool]] = field(default_factory=dict)  # ASR segment → (文本, 是否 final)
    dirty: bool = True  # 输入变了，需要（重新）判定
    result: GateResult | None = None

    def overlaps(self, ev: AsrEvent) -> bool:
        end = self.u.vad_ended_at_ms
        return ev.segment_end_ms > self.u.start_ms and (end is None or ev.segment_start_ms < end)


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
        # 打断门禁（设计 §9）
        self._utterance_id = 0
        self._gate: _GateUtterance | None = None  # 正在评估的那段话
        self._decided: deque[_GateUtterance] = deque(maxlen=4)  # 最近判为噪声/搭腔的话（迟到的词）
        self._decide_task: asyncio.Task | None = None
        self._recent_pcm: deque[bytes] = deque(maxlen=cfg.pause_min_speech_frames)
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
        self._closing = False
        self._closed_written = False
        self._overdue: set[str] = set()  # 出现在 close_timeout 中的任务名
        self._text_max_depth = 0  # 已清理的 generation 的 text 队列最大长度
        log.add_listener(self._on_log_event)

    # ---- 公共接口 ----

    def close(self, reason: CloseReason = CloseReason.EXPLICIT_CLOSE) -> None:
        if self._close_reason is None:
            self._close_reason = reason
        self._stop_event.set()

    async def run(self) -> CloseReason:
        """§10.4 / §13：TaskGroup 外层用 except* 走异常路径；关闭序列的收尾放在 finally 中并用
        shield 保护（外部取消 run() 时也能完整写出 session_closed）。"""
        error = None
        try:
            async with asyncio.TaskGroup() as tg:
                self._tg = tg
                self._spawn(self._audio_pump(), "session/audio_pump")
                self._spawn(self._asr_consumer(), "session/asr_consumer")
                self._spawn(self._control(), "session/control")
                self._spawn(self._player.run(), "session/player")
                await self._stop_event.wait()
                self._begin_close()  # 第 1～3 步
                await self._stop_tasks()  # 第 4、5 步
        except* Exception as eg:
            # 主动关闭过程中 session 级任务抛异常，同样按异常路径处理：reason 覆盖为 session_task_failed
            e = eg.exceptions[0]
            error = f"{type(e).__name__}: {e}"
            self._close_reason = CloseReason.SESSION_TASK_FAILED
        finally:
            self._tg = None
            closer = asyncio.create_task(self._finish_close(error), name="session/close")
            await asyncio.shield(closer)
        return self._close_reason

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

    def _begin_close(self) -> None:
        """关闭第 1～3 步（同步、幂等）：CLOSING → 作废 generation → 停播并关闭播放器。"""
        if self._closing:
            return
        self._closing = True
        audible = self.state in (SessionState.PLAYING, SessionState.PAUSED)
        self.state = SessionState.CLOSING
        # 与打断相同：先停播（补记关闭前已播帧的 progress、附 Playback Truth），再作废
        cur = self._player.current_generation
        if audible and cur is not None:
            self._player.stop_generation(cur, "session_closed")
        gid = self.active_generation_id
        if gid is not None:
            self.invalidate(self._gens[gid], "session_closed")
        self._player.close()

    async def _stop_tasks(self) -> None:
        """关闭第 4、5 步：取消 session/* 循环，等 Session 自己登记的任务（session/* 与各 supervisor）
        全部退出；每超过一个 close_grace_ms 写一条 close_timeout，并对仍未退出的任务再 cancel() 一次。
        supervisor 已在自己 TaskGroup 的 __aexit__ 中时，取消它不会再传给子任务，所以子任务由
        supervisor 交出、直接再取消。"""
        for t in self._tasks:
            if t.get_name().startswith("session/"):
                t.cancel()
        rnd = 0
        while self._tasks:
            _, pending = await asyncio.wait(set(self._tasks), timeout=self.cfg.close_grace_ms / 1000)
            pending = [t for t in pending if not t.done()]
            if not pending:
                continue
            for gid in sorted(self._running_sups):
                pending += self._sups[gid].pending_tasks()
            pending.sort(key=asyncio.Task.get_name)
            rnd += 1
            names = [t.get_name() for t in pending]
            self._overdue.update(names)
            self._log.emit(CLOSE_TIMEOUT, turn_id=self._tid, round=rnd, tasks=names)
            for t in pending:
                t.cancel()

    async def _finish_close(self, error: str | None) -> None:
        """关闭第 6、7 步（异常路径与外部取消时先补做第 1～3 步的同步部分）。只写一次 session_closed。"""
        if self._closed_written:
            return
        self._begin_close()
        if self._close_reason is None:  # 外部直接取消了 run()
            self._close_reason = CloseReason.EXPLICIT_CLOSE
        residual = {name: q.close() for name, q in self.queues.items()}
        residual["text"] = sum(g.text_queue.close() for g in self._gens.values())
        max_depth = {name: q.max_depth for name, q in self.queues.items()}
        max_depth["text"] = max([self._text_max_depth,
                                 *(g.text_queue.max_depth for g in self._gens.values())])
        self.state = SessionState.CLOSED
        self._closed_written = True
        self._log.emit(SESSION_CLOSED, turn_id=self._tid, reason=str(self._close_reason),
                       residual=residual, queue_max_depth=max_depth,
                       overdue_task_count=len(self._overdue),
                       **({"error": error} if error else {}))

    # ---- session 级循环 ----

    async def _audio_pump(self) -> None:
        asr_in = self.queues["asr_in"]
        async for frame in self._source.frames():
            if self.cfg.log_audio_frames:
                self._log.emit(AUDIO_FRAME, turn_id=self._tid, seq=frame.seq, ts_ms=frame.ts_ms)
            edge = self._tracker.feed(frame, self._p.vad.process(frame))
            if edge is not None:
                self._on_speech_edge(edge)
            self._gate_on_frame(frame)
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
            # 与门禁中的话时间重叠 → 只归门禁；否则只有 LISTENING 且轮次已开启时才进 cascade
            g = self._gate_owner(ev)
            if g is not None:
                self._gate_on_asr(g, ev)
            elif self.state is SessionState.LISTENING and self._turn_open:
                self._cascade.on_text(ev.text, ev.kind == "final")

    async def _control(self) -> None:
        while True:
            await asyncio.sleep(FRAME_MS / 1000)
            now = now_ms()
            self._gate_tick(now)
            self._control_tick(now)

    # ---- 判停与轮次 ----

    def _on_speech_edge(self, edge: SpeechEdge) -> None:
        listening = self.state is SessionState.LISTENING
        if edge.kind == "start" and listening and not self._turn_open:
            self._open_turn()
        self._log.emit(SPEECH_START if edge.kind == "start" else SPEECH_END, turn_id=self._tid,
                       ts_ms=edge.ts_ms, epoch=edge.epoch)
        g = self._gate
        if edge.kind == "end" and g is not None and g.u.vad_ended_at_ms is None:
            g.u.vad_ended_at_ms = edge.ts_ms
            self._refresh_text(g)
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
                                   is_closed=lambda: self._closing,
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
        gen = self._gens.pop(gid, None)
        if gen is not None:
            self._text_max_depth = max(self._text_max_depth, gen.text_queue.max_depth)
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
        carry, self._gate = self._gate, None
        if carry is not None:
            # 门禁中的那段话（打断者，或 generation 失败时正在评估的话）成为下一轮
            self._hand_to_cascade(carry)
        elif self._tracker.in_speech:
            # 用户正说到一半（播放期间开口）：这段话直接成为下一轮，后续的累计 ASR 文本归入新轮次
            self._open_turn()
            self._cascade.on_speech_edge(
                SpeechEdge("start", self._tracker.speech_start_ms, self._tracker.epoch))

    # ---- 打断门禁（设计 §9） ----

    def _gate_on_frame(self, frame: Frame) -> None:
        """连续 pause_min_speech_frames 帧语音：PLAYING 时暂停播放，PENDING 时保持 held，开始评估。"""
        self._recent_pcm.append(frame.pcm)
        run = self._tracker.speech_frames_run
        g = self._gate
        if g is not None:
            if run and g.u.vad_ended_at_ms is None:
                g.u.append_audio(frame.pcm, self.cfg)
            elif run >= self.cfg.pause_min_speech_frames:
                # 尚未判定时用户又开口：同一段话的 VAD 区间重新打开，补上达到门槛前的几帧
                g.u.vad_ended_at_ms = None
                for pcm in self._recent_pcm:
                    g.u.append_audio(pcm, self.cfg)
                g.epoch = self._tracker.epoch
                self._refresh_text(g)
            return
        if run < self.cfg.pause_min_speech_frames or self.state not in (SessionState.PLAYING,
                                                                       SessionState.PENDING):
            return
        self._utterance_id += 1
        u = Utterance(self._utterance_id, frame.ts_ms - (run - 1) * FRAME_MS, None, "", None,
                      bytearray())
        for pcm in self._recent_pcm:
            u.append_audio(pcm, self.cfg)
        gid = self.active_generation_id
        self._gate = _GateUtterance(u, self._tracker.epoch, gid)
        if self.state is SessionState.PLAYING:
            self._player.pause()
            self.state = SessionState.PAUSED
        else:
            self._player.hold(gid)  # 还没出声：门控保持 held，没有声音要停

    def _gate_owner(self, ev: AsrEvent) -> _GateUtterance | None:
        if self._gate is not None and self._gate.overlaps(ev):
            return self._gate
        return next((g for g in self._decided if g.overlaps(ev)), None)

    def _gate_on_asr(self, g: _GateUtterance, ev: AsrEvent) -> None:
        g.segments[ev.segment_id] = (ev.text, ev.kind == "final")
        self._refresh_text(g)

    @staticmethod
    def _refresh_text(g: _GateUtterance) -> None:
        """按 segment 顺序汇总文本。VAD 仍在说话时不算有 final：判定不会进入搭腔评分。"""
        u = g.u
        u.partial_text = " ".join(t for t, _ in g.segments.values() if t)
        done = u.vad_ended_at_ms is not None and all(f for _, f in g.segments.values())
        u.final_text = u.partial_text if done and g.segments else None
        g.dirty = True

    def _gate_tick(self, now: int) -> None:
        """每次最多一个在途判定；只在输入变化或等待噪声超时时才重新判定（不多调附和评分器）。"""
        if self._decide_task is not None:
            return
        g = self._gate
        if g is not None:
            u = g.u
            waiting_timeout = (u.final_text is None and u.vad_ended_at_ms is not None
                               and now - u.vad_ended_at_ms >= self.cfg.noise_timeout_ms)
            if g.dirty or waiting_timeout:
                self._spawn_decide(g, now, late=False)
            return
        # 迟到的词（§9.3）：只重判那次回复仍在播的噪声；判为搭腔的话直接丢弃
        late = next((d for d in self._decided if d.dirty and d.result is GateResult.NOISE
                     and d.generation_id == self.active_generation_id), None)
        if late is not None:
            self._spawn_decide(late, now, late=True)

    def _spawn_decide(self, g: _GateUtterance, now: int, late: bool) -> None:
        g.dirty = False
        self._decide_task = self._spawn(self._decide(g, now, late), "session/gate-decide")

    async def _decide(self, g: _GateUtterance, now: int, late: bool) -> None:
        try:
            result, rule, details = await decide(g.u, now, self.cfg, self._p.backchannel)
        finally:
            self._decide_task = None
        if late:
            # 只在同一个 generation 仍在播、且门禁空闲时处理；不是打断就丢弃
            if (result is not GateResult.INTERRUPT or self._gate is not None
                    or g not in self._decided or g.generation_id != self.active_generation_id):
                return
            self._decided.remove(g)
            self._gate = g
            if self.state is SessionState.PLAYING:
                self._player.pause()
                self.state = SessionState.PAUSED
        elif self._gate is not g:
            return  # 判定期间轮次已结束，这段话已交给 cascade
        elif result is GateResult.PENDING or (result is not GateResult.INTERRUPT
                                              and g.u.vad_ended_at_ms is None):
            return  # VAD 仍判定在说话：保持暂停
        gen = self._gens[g.generation_id]
        self._log.emit(BARGEIN_DECISION, turn_id=gen.turn_id, generation_id=gen.id,
                       result=str(result), rule=rule, utterance_id=g.u.utterance_id,
                       words=details["words"], scores=details["scores"],
                       scorer=details.get("scorer"), late_interrupt=late)
        if result is GateResult.INTERRUPT:
            self._interrupt(gen)
            return
        # 搭腔 / 噪声：丢弃转写，从暂停的样本位继续
        g.result = result
        self._decided.append(g)
        self._gate = None
        if self.state is SessionState.PAUSED:
            self._player.resume()
            self.state = SessionState.PLAYING
        else:
            self._player.release(gen.id)

    def _interrupt(self, gen: Generation) -> None:
        if self.state is SessionState.PENDING:
            # 用户继续说：作废 generation，同一轮继续累计文本（不结束轮次、不提交上下文）
            g, self._gate = self._gate, None
            self._player.unhold(gen.id)  # 播放器不再持有，supervisor 退出后即可清理
            self.invalidate(gen, "user_continued")
            self._prune(gen.id)
            self.state = SessionState.LISTENING
            self._hand_to_cascade(g)
            return
        self._player.stop_generation(gen.id, "interrupted")
        self.invalidate(gen, "barge_in")
        self._end_turn("interrupted", gen)  # 门禁中的这段话在 _end_turn 里成为新一轮

    def _hand_to_cascade(self, g: _GateUtterance) -> None:
        """把这段话补放进 cascade：开口边沿、已有文本，以及（已说完时）speech_end 边沿。"""
        if not self._turn_open:
            self._open_turn()
        u = g.u
        self._cascade.on_speech_edge(SpeechEdge("start", u.start_ms, g.epoch))
        for text, is_final in g.segments.values():  # 逐段补放：先各段 final，最后一段可能仍是 partial
            self._cascade.on_text(text, is_final)
        if u.vad_ended_at_ms is not None:
            self._cascade.on_speech_edge(SpeechEdge("end", u.vad_ended_at_ms, g.epoch))
