import asyncio

import pytest

from voice_runtime.config import RuntimeConfig
from voice_runtime.events import (AUDIO_ENQUEUED, CANCEL_GRACE_EXCEEDED, PROVIDER_TIMEOUT,
                                  STALE_EVENT_DROPPED, TASK_FAILED, TTS_CHUNK, EventLog)
from voice_runtime.generation import GenerationSupervisor, Generation, GenState
from voice_runtime.playback import PlaybackEOS, PlaybackFrame
from voice_runtime.providers.fake import FakeLLM, FakeTTS, StreamFaults
from voice_runtime.queues import BoundedQueue
from voice_runtime.truth import TruthLedger
from voice_runtime.vloop import now_ms, run

REPLY = "hello there how are you today"
_LOGS: list[EventLog] = []


@pytest.fixture(autouse=True)
def _close_logs():
    yield
    while _LOGS:
        _LOGS.pop().close()


class Rig:
    """直接构造 supervisor；is_active 是一个简单开关，invalidate 由测试模拟 Session 完成。"""

    def __init__(self, tmp_path, *, reply=REPLY, llm=None, tts=None, playback_capacity=75,
                 cfg=None, llm_provider=None):
        self.events: list[dict] = []
        self.log = EventLog(tmp_path / "e.jsonl", "s")
        _LOGS.append(self.log)
        self.log.add_listener(self.events.append)
        self.playback = BoundedQueue("playback", playback_capacity)
        self.gen = Generation(id=1, turn_id=7, user_text="hi", speculative=False,
                              state=GenState.ACTIVE, text_queue=BoundedQueue("text-1", 64),
                              ledger=TruthLedger(1))
        self.active = True
        self.failed: list[str] = []
        self.sup = GenerationSupervisor(
            self.gen, cfg or RuntimeConfig(), self.log,
            llm_provider or FakeLLM({}, reply, llm or StreamFaults(100, 100, jitter_ms=0)),
            FakeTTS(tts or StreamFaults(100, 100, jitter_ms=0)),
            [{"role": "user", "content": "hi"}], self.playback,
            is_active=lambda g: self.active and g is self.gen,
            on_failed=self.on_failed)

    def on_failed(self, gen, reason):
        self.failed.append(reason)
        self.invalidate()

    def invalidate(self):
        """模拟 Session.invalidate 的同步段：切身份、清旧帧、关 text 队列，然后发取消信号。"""
        self.active = False
        self.gen.state = GenState.CANCELLED
        self.playback.purge(lambda item: item.generation_id == self.gen.id)
        self.gen.text_queue.close()
        self.sup.signal_cancel()

    def of(self, event_type, **match):
        return [e for e in self.events if e["event_type"] == event_type
                and all(e["payload"].get(k) == v for k, v in match.items())]

    def drain(self) -> list:
        out = []
        while (item := self.playback.get_nowait()) is not None:
            out.append(item)
        return out


def test_happy_path_enqueues_frames_then_eos(tmp_path):
    async def main():
        r = Rig(tmp_path)
        await r.sup.run()
        items = r.drain()
        frames = [i for i in items if isinstance(i, PlaybackFrame)]
        assert items[-1] == PlaybackEOS(1, truncated=False)
        assert [f.frame_index for f in frames] == list(range(len(frames)))
        assert all(len(f.pcm) == 640 and f.generation_id == 1 for f in frames)
        chunks = r.of(TTS_CHUNK)
        enq = r.of(AUDIO_ENQUEUED)
        assert [e["payload"]["chunk_id"] for e in chunks] == list(range(len(chunks)))
        assert [e["payload"]["chunk_id"] for e in enq] == list(range(len(chunks)))
        assert sum(e["payload"]["frames"] for e in enq) == len(frames)
        assert all(e["payload"]["queue"] == "playback" for e in enq)
        # 540ms 语音 = 27 帧
        assert len(frames) == 27
        assert {f.chunk_id for f in frames} == set(range(len(chunks)))
        summary = r.gen.ledger.summary()
        assert summary["full_text"] == "hello there how are you today "
        assert all(c["state"] == "enqueued" for c in summary["chunks"])
        assert all(e["turn_id"] == 7 and e["generation_id"] == 1 for e in r.events)
        assert not r.of(STALE_EVENT_DROPPED) and not r.of(PROVIDER_TIMEOUT) and not r.failed

    run(main())


def test_backpressure_blocks_tts_at_capacity_without_timeout(tmp_path):
    async def main():
        r = Rig(tmp_path, reply=" ".join(["word"] * 30), playback_capacity=5)
        task = asyncio.create_task(r.sup.run(), name="gen-1/supervisor")
        await asyncio.sleep(3)
        assert not r.of(PROVIDER_TIMEOUT)
        assert r.playback.max_depth == 5
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(main())


def test_cancel_drains_late_chunks_as_stale_late(tmp_path):
    async def main():
        r = Rig(tmp_path, tts=StreamFaults(100, 100, jitter_ms=0, late_after_cancel_ms=(50, 400)))
        task = asyncio.create_task(r.sup.run(), name="gen-1/supervisor")
        await asyncio.sleep(0.35)  # 第一个 TTS chunk（t=200）已入队
        assert r.of(AUDIO_ENQUEUED)
        r.invalidate()
        cancelled_at = now_ms()
        await task
        late = r.of(STALE_EVENT_DROPPED, gate="2", phase="late", unit="chunk")
        assert len(late) == 2
        eos = r.of(STALE_EVENT_DROPPED, unit="eos")
        assert [e["payload"] for e in eos] == [{"gate": "2", "phase": "late", "unit": "eos"}]
        assert [e["timestamp"] - cancelled_at for e in late] == [50, 400]
        assert r.playback.depth == 0  # purge 之后没有新的帧进入
        assert not r.of(STALE_EVENT_DROPPED, phase="rejected_on_wake")
        assert not r.of(CANCEL_GRACE_EXCEEDED)
        summary = r.gen.ledger.summary()
        assert [c["state"] for c in summary["chunks"][-2:]] == ["dropped", "dropped"]

    run(main())


def test_blocked_put_rejected_on_wake_is_not_counted_late(tmp_path):
    async def main():
        r = Rig(tmp_path, playback_capacity=5)
        task = asyncio.create_task(r.sup.run(), name="gen-1/supervisor")
        await asyncio.sleep(0.35)  # 第一个 chunk（t=200，10 帧）在第 6 帧上阻塞
        assert r.playback.depth == 5
        assert not r.of(AUDIO_ENQUEUED)
        r.invalidate()  # purge 唤醒阻塞的 put，醒来后重校验被拒
        await task
        wake = r.of(STALE_EVENT_DROPPED, phase="rejected_on_wake")
        assert len(wake) == 1
        assert wake[0]["payload"] == {"gate": "2", "phase": "rejected_on_wake", "unit": "chunk",
                                      "chunk_id": 0}
        assert not r.of(STALE_EVENT_DROPPED, phase="late", unit="chunk")
        assert r.playback.depth == 0
        chunk0 = r.gen.ledger.summary()["chunks"][0]
        assert chunk0["state"] == "dropped" and chunk0["enqueued"]

    run(main())


def test_grace_exceeded_cancels_bridges_and_logs(tmp_path):
    async def main():
        r = Rig(tmp_path, tts=StreamFaults(100, 100, jitter_ms=0, late_after_cancel_ms=(5000,)))
        task = asyncio.create_task(r.sup.run(), name="gen-1/supervisor")
        await asyncio.sleep(0.35)
        r.invalidate()
        cancelled_at = now_ms()
        await task
        assert now_ms() - cancelled_at == 1000
        exceeded = r.of(CANCEL_GRACE_EXCEEDED)
        assert len(exceeded) == 1
        assert exceeded[0]["payload"]["tasks"] == ["gen-1/tts_bridge"]
        assert not r.of(STALE_EVENT_DROPPED, phase="late")

    run(main())


def test_first_chunk_timeout_calls_on_failed(tmp_path):
    async def main():
        r = Rig(tmp_path, llm=StreamFaults(3000, 100, jitter_ms=0))
        await r.sup.run()
        timeouts = r.of(PROVIDER_TIMEOUT)
        assert [(e["timestamp"], e["payload"]) for e in timeouts] == [
            (2000, {"provider": "llm", "first": True})]
        assert r.failed == ["first_chunk_timeout"]
        assert r.gen.cancelled_at_ms == 2000
        assert not r.of(TTS_CHUNK)

    run(main())


def test_tts_first_timeout_counts_from_first_text(tmp_path):
    async def main():
        # LLM 首包 1500ms，TTS 首包 1200ms（从第一段文本起算）：超时在 1500+1000 时触发
        r = Rig(tmp_path, llm=StreamFaults(1500, 100, jitter_ms=0),
                tts=StreamFaults(1200, 100, jitter_ms=0))
        await r.sup.run()
        timeouts = r.of(PROVIDER_TIMEOUT)
        assert [(e["timestamp"], e["payload"]) for e in timeouts] == [
            (2500, {"provider": "tts", "first": True})]
        assert r.failed == ["first_chunk_timeout"]

    run(main())


def test_mid_stream_timeout_finishes_with_truncated_eos_and_gate_finished(tmp_path):
    async def main():
        r = Rig(tmp_path, llm=StreamFaults(100, 100, jitter_ms=0, hang_after=3),
                tts=StreamFaults(100, 100, jitter_ms=0, late_after_cancel_ms=(50,)))
        await r.sup.run()
        timeouts = r.of(PROVIDER_TIMEOUT)
        assert [(e["timestamp"], e["payload"]) for e in timeouts] == [
            (1300, {"provider": "llm", "first": False, "discarded_text_pieces": 0})]
        assert r.gen.state is GenState.UPSTREAM_CLOSED
        assert not r.failed
        finished = r.of(STALE_EVENT_DROPPED, gate="finished")
        assert len(finished) == 1
        assert finished[0]["payload"]["phase"] == "late"
        assert finished[0]["timestamp"] == 1350
        items = r.drain()
        assert items[-1] == PlaybackEOS(1, truncated=True)
        assert all(isinstance(i, PlaybackFrame) for i in items[:-1]) and len(items) > 1
        assert not r.of(CANCEL_GRACE_EXCEEDED)

    run(main())


def test_llm_exception_reports_task_failed_and_does_not_propagate(tmp_path):
    async def main():
        r = Rig(tmp_path, llm=StreamFaults(100, 100, jitter_ms=0, raise_at=1))
        await r.sup.run()
        failed = r.of(TASK_FAILED)
        assert [e["payload"] for e in failed] == [
            {"task": "gen-1/llm_bridge", "error": "RuntimeError: fake failure"}]
        assert r.failed == ["task_failed"]

    run(main())


def test_tts_mid_stream_timeout_on_final_flush_finishes(tmp_path):
    async def main():
        # 文本 EOS 在 t=300 交出后，TTS 的最后一个 chunk 要到 5200 才来：按 inter_chunk 超时
        r = Rig(tmp_path, reply="hello there how", tts=StreamFaults(100, 5000, jitter_ms=0))
        await r.sup.run()
        timeouts = r.of(PROVIDER_TIMEOUT)
        assert [(e["timestamp"], e["payload"]) for e in timeouts] == [
            (1300, {"provider": "tts", "first": False, "discarded_text_pieces": 0})]
        assert r.gen.state is GenState.UPSTREAM_CLOSED and not r.failed
        assert now_ms() == 1300
        assert r.drain()[-1] == PlaybackEOS(1, truncated=True)

    run(main())


def test_tts_time_starved_for_text_is_not_counted(tmp_path):
    async def main():
        # 首个 chunk 在 t=200；之后 4 个短词每 600ms 来一个，TTS 等文本 2300ms 才凑够下一个 chunk，
        # 超过 inter_chunk_timeout_ms，但这段时间是在等 LLM，不算 TTS 超时
        r = Rig(tmp_path, reply="extraordinarily a b c d e", llm=StreamFaults(100, 600, jitter_ms=0))
        await r.sup.run()
        assert not r.of(PROVIDER_TIMEOUT) and not r.failed
        assert [e["timestamp"] for e in r.of(TTS_CHUNK)] == [200, 2500, 3100]
        assert r.drain()[-1] == PlaybackEOS(1, truncated=False)

    run(main())


class _TimeoutRaisingStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0.1)
        raise TimeoutError("provider deadline")

    def cancel(self):
        pass


class _TimeoutRaisingLLM:
    def start(self, messages):
        return _TimeoutRaisingStream()


def test_provider_raised_timeout_error_is_task_failure_not_provider_timeout(tmp_path):
    async def main():
        r = Rig(tmp_path, llm_provider=_TimeoutRaisingLLM())
        await r.sup.run()
        assert not r.of(PROVIDER_TIMEOUT)
        assert [e["payload"] for e in r.of(TASK_FAILED)] == [
            {"task": "gen-1/llm_bridge", "error": "TimeoutError: provider deadline"}]
        assert r.failed == ["task_failed"]

    run(main())
