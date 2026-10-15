import asyncio

import pytest
from helpers import build_session, run_scenario

from voice_runtime.config import RuntimeConfig
from voice_runtime.events import EventLog
from voice_runtime.providers.fake import StreamFaults
from voice_runtime.scenarios import REQUEST, Scenario
from voice_runtime.script import AfterEvent, Disconnect, Noise, Speech
from voice_runtime.session import CloseReason, SessionState
from voice_runtime.vloop import run

LLM = StreamFaults(300, 50)
TTS = StreamFaults(200, 40)
REPLY = "Sure, a table for two is booked for Thursday at seven in the evening."


def of(events, event_type, **match):
    return [e for e in events if e["event_type"] == event_type
            and all(e["payload"].get(k) == v for k, v in match.items())]


def scenario(name, segments, *, llm=LLM, close_after=("context_committed", 2, 100), **kw):
    return Scenario(name=name, segments=segments, llm_replies={}, default_reply=REPLY,
                    llm_faults=llm, tts_faults=TTS, close_after=close_after, **kw)


def session_tasks() -> list[str]:
    return [t.get_name() for t in asyncio.all_tasks()
            if not t.done() and t.get_name().startswith(("session/", "gen-"))]


def drive(tmp_path, sc, body, cfg=None):
    """在虚拟 loop 上跑 session；body(session, run_task) 负责触发关闭并返回结果。"""
    log = EventLog(tmp_path / f"{sc.name}.jsonl", "s1")
    events: list[dict] = []
    log.add_listener(events.append)
    session, sink = build_session(sc, cfg or RuntimeConfig(), log)

    async def main():
        task = asyncio.create_task(session.run(), name="test/run")
        return await body(session, task, events)

    return run(main()), events, session, sink


async def wait_for(events, event_type, n=1):
    while len(of(events, event_type)) < n:
        await asyncio.sleep(0.02)


def test_no_running_tasks_after_close(tmp_path):
    async def body(session, task, events):
        await wait_for(events, "playback_started")
        await asyncio.sleep(0.3)
        session.close()
        reason = await task
        return reason, session_tasks()

    (reason, alive), events, _, _ = drive(tmp_path, scenario("tasks", [Speech(REQUEST)]), body)
    assert reason is CloseReason.EXPLICIT_CLOSE
    assert alive == []
    closed = of(events, "session_closed")
    assert len(closed) == 1 and closed[0]["payload"]["overdue_task_count"] == 0
    assert closed[0]["payload"]["reason"] == "explicit_close"
    assert set(closed[0]["payload"]["residual"]) == set(closed[0]["payload"]["queue_max_depth"])
    # 在播时关闭：先停播（补记 progress、附 Playback Truth），再作废，然后才写 session_closed
    stopped = of(events, "playback_stopped", reason="session_closed")
    cancelled = of(events, "generation_cancelled", reason="session_closed")
    assert len(stopped) == 1 and len(cancelled) == 1
    assert stopped[0]["sequence_number"] < cancelled[0]["sequence_number"] \
        < closed[0]["sequence_number"]
    assert "playback_truth" in stopped[0]["payload"]


def test_close_mid_playback_counts_no_stale_played_frames(tmp_path):
    """关闭前已播的帧在作废之前补记 progress，不能被算成 stale_chunk_played。"""
    sc = scenario("mid_close", [Speech(REQUEST)], close_after=("playback_started", 1, 310))
    r = run_scenario(sc, tmp_path)
    assert r.state_at_close is SessionState.PLAYING
    stopped = of(r.events, "playback_stopped", reason="session_closed")
    cancelled = of(r.events, "generation_cancelled", reason="session_closed")
    assert len(stopped) == 1 and len(cancelled) == 1
    assert stopped[0]["sequence_number"] < cancelled[0]["sequence_number"]
    progress = of(r.events, "playback_progress")
    assert progress and progress[-1]["sequence_number"] < cancelled[0]["sequence_number"]
    assert r.metrics["stale_chunk_played"] == 0


def test_swallowing_provider_triggers_recancel_rounds(tmp_path):
    # 光有 hang_after 时，取消信号 cancel() 会直接唤醒假流并结束，CancelledError 根本到不了它；
    # 加 late_after_cancel_ms 让它在 cancel() 之后仍在等（模拟不配合取消的 provider）：
    # cancel_grace（1000ms）看门狗的第一次取消被吞掉，只能靠关闭流程的 close_timeout 轮次再取消。
    llm = StreamFaults(300, 50, hang_after=1, swallow_first_cancel=True,
                       late_after_cancel_ms=(10_000,))
    sc = scenario("swallow", [Speech(REQUEST)], llm=llm)

    async def body(session, task, events):
        await wait_for(events, "llm_chunk")
        await asyncio.sleep(0.2)  # LLM 吐出 1 个词后卡住；chunk 间隔超时（1000ms）之前关闭
        session.close()
        reason = await task
        return reason, session_tasks()

    (reason, alive), events, _, _ = drive(tmp_path, sc, body)
    assert reason is CloseReason.EXPLICIT_CLOSE
    assert alive == []
    timeouts = of(events, "close_timeout")
    assert len(timeouts) >= 1
    assert [t["payload"]["round"] for t in timeouts] == list(range(1, len(timeouts) + 1))
    assert all(t["payload"]["tasks"] for t in timeouts)
    assert "gen-1/llm_bridge" in timeouts[0]["payload"]["tasks"]
    assert of(events, "cancel_grace_exceeded")  # 第一次取消（看门狗）被吞掉了
    assert not of(events, "llm_chunk")[1:]  # 迟到项（cancel 后 10s）没有等到
    closed = of(events, "session_closed")
    assert len(closed) == 1 and closed[0]["payload"]["overdue_task_count"] >= 1
    assert closed[0]["sequence_number"] > timeouts[-1]["sequence_number"]
    grace = RuntimeConfig().close_grace_ms
    assert timeouts[0]["timestamp"] - of(events, "generation_cancelled")[0]["timestamp"] >= grace


def test_external_cancel_of_run_still_writes_session_closed(tmp_path):
    async def body(session, task, events):
        await wait_for(events, "playback_started")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return session_tasks()

    alive, events, session, sink = drive(tmp_path, scenario("ext", [Speech(REQUEST)]), body)
    assert alive == []
    closed = of(events, "session_closed")
    assert len(closed) == 1 and closed[0]["payload"]["reason"] == "explicit_close"
    assert session.state is SessionState.CLOSED and sink.closed


def test_session_task_exception_closes_with_reason(tmp_path):
    class BoomVAD:
        def process(self, frame):
            if frame.seq == 10:
                raise RuntimeError("boom")
            return 0.05

    log = EventLog(tmp_path / "boom.jsonl", "s1")
    events: list[dict] = []
    log.add_listener(events.append)
    session, sink = build_session(scenario("boom", [Speech(REQUEST)]), RuntimeConfig(), log)
    session._p.vad = BoomVAD()

    async def main():
        reason = await session.run()
        return reason, session_tasks()

    reason, alive = run(main())
    assert reason is CloseReason.SESSION_TASK_FAILED and alive == []
    closed = of(events, "session_closed")
    assert len(closed) == 1
    assert closed[0]["payload"]["reason"] == "session_task_failed"
    assert closed[0]["payload"]["error"] == "RuntimeError: boom"
    assert sink.closed


def test_disconnect_closes_with_client_disconnect(tmp_path):
    sc = scenario("disconnect", [Speech(REQUEST), AfterEvent("playback_started", 300),
                                 Disconnect()], close_after=("session_closed", 1, 0))
    r = run_scenario(sc, tmp_path)
    assert r.reason is CloseReason.CLIENT_DISCONNECT
    closed = of(r.events, "session_closed")
    assert len(closed) == 1 and closed[0]["payload"]["reason"] == "client_disconnect"
    assert "error" not in closed[0]["payload"]
    assert of(r.events, "playback_stopped", reason="session_closed")


def test_close_twice_and_close_while_paused_is_idempotent(tmp_path):
    sc = scenario("paused", [Speech(REQUEST), AfterEvent("playback_started", 1000), Noise(200)])

    async def body(session, task, events):
        await wait_for(events, "playback_stopped")
        assert session.state is SessionState.PAUSED
        session.close()
        session.close(CloseReason.CLIENT_DISCONNECT)  # 第二次关闭不改 reason
        reason = await task
        session.close()  # run 结束之后再关也无害
        return reason

    reason, events, session, sink = drive(tmp_path, sc, body)
    assert reason is CloseReason.EXPLICIT_CLOSE
    closed = of(events, "session_closed")
    assert len(closed) == 1 and closed[0] is events[-1]
    assert all(ts <= closed[0]["timestamp"] for ts, _, _ in sink.writes)
    assert [s["payload"]["reason"] for s in of(events, "playback_stopped")] == \
        ["paused_pending_decision", "session_closed"]
    assert not of(events, "playback_resumed")


def test_concurrent_invalidate_is_idempotent(tmp_path):
    async def body(session, task, events):
        while session.active_generation_id is None:
            await asyncio.sleep(0.02)
        gen = session._gens[session.active_generation_id]
        for _ in range(3):  # 同一个 tick 内连续 3 次
            session.invalidate(gen, "test")
        session.close()
        return await task

    # 关闭预生成：只看判停后服务本轮的 generation，不混入被替换的预生成
    _, events, _, _ = drive(tmp_path, scenario("concurrent", [Speech(REQUEST)]), body,
                            RuntimeConfig(speculation_enabled=False))
    cancelled = of(events, "generation_cancelled")
    assert [c["payload"]["duplicate"] for c in cancelled] == [False, True, True]
    assert len(of(events, "stale_event_dropped", gate="purge")) == 1


def test_external_cancel_with_swallowing_provider_closes_cleanly(tmp_path):
    llm = StreamFaults(300, 50, hang_after=1, swallow_first_cancel=True,
                       late_after_cancel_ms=(10_000,))

    async def body(session, task, events):
        await wait_for(events, "llm_chunk")
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return session_tasks()

    alive, events, _, _ = drive(tmp_path, scenario("ext_swallow", [Speech(REQUEST)], llm=llm),
                                body)
    assert alive == []
    assert not of(events, "task_failed") and not of(events, "generation_failed")
    closed = of(events, "session_closed")
    assert len(closed) == 1 and closed[0]["payload"]["reason"] == "explicit_close"
