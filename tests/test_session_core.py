import asyncio

from helpers import build_session, run_scenario

from voice_runtime.config import RuntimeConfig
from voice_runtime.events import EventLog
from voice_runtime.providers.fake import StreamFaults
from voice_runtime.scenarios import Scenario
from voice_runtime.script import AfterEvent, Silence, Speech
from voice_runtime.session import CloseReason, SessionState
from voice_runtime.vloop import run

LLM = StreamFaults(300, 50)
TTS = StreamFaults(200, 40)


def of(events, event_type, **match):
    return [e for e in events if e["event_type"] == event_type
            and all(e["payload"].get(k) == v for k, v in match.items())]


def scenario(name, segments, *, reply="Sure, a table for two is booked.", replies=None,
             llm=LLM, close_after=("context_committed", 2, 100), **kw):
    return Scenario(name=name, segments=segments, llm_replies=replies or {},
                    default_reply=reply, llm_faults=llm, tts_faults=TTS,
                    close_after=close_after, **kw)


def test_complete_utterance_flows_to_playback_and_ends_turn(tmp_path):
    r = run_scenario(scenario("complete", [Speech("Book a table for two please.")]), tmp_path)
    ev = r.events
    order = ["speech_end", "endpoint_committed", "llm_chunk", "tts_chunk", "audio_enqueued",
             "playback_started", "playback_stopped", "context_committed"]
    first = [next(e["sequence_number"] for e in ev if e["event_type"] == t) for t in order]
    assert first == sorted(first)
    assert of(ev, "playback_stopped")[0]["payload"]["reason"] == "completed"
    ep = of(ev, "endpoint_committed")[0]["payload"]
    assert ep["text"] == "Book a table for two please." and ep["text_source"] == "final"
    assert ep["reason"] == "score_threshold" and ep["level"] == 1
    assert {"decided_at", "p", "threshold"} <= ep.keys()
    user, assistant = of(ev, "context_committed")
    assert user["payload"]["role"] == "user"
    assert user["payload"]["text"] == "Book a table for two please."
    assert assistant["payload"]["role"] == "assistant"
    assert assistant["payload"]["text"] == "Sure, a table for two is booked."
    assert assistant["payload"]["interrupted"] is False
    assert list(r.session.context) == [
        {"role": "user", "content": "Book a table for two please."},
        {"role": "assistant", "content": "Sure, a table for two is booked."}]
    assert r.state_at_close is SessionState.LISTENING
    assert r.session.active_generation_id is None
    assert r.reason is CloseReason.EXPLICIT_CLOSE
    assert of(ev, "session_closed", reason="explicit_close")
    assert r.sink.writes and r.sink.closed


def test_hesitation_does_not_commit_during_pause(tmp_path):
    segs = [Speech("Please book it for"), Silence(700), Speech("Wednesday afternoon.")]
    r = run_scenario(scenario("hesitation", segs), tmp_path)
    ev = r.events
    second_start = of(ev, "script_marker", segment="speech", phase="start", index=2)[0]["timestamp"]
    commits = of(ev, "endpoint_committed")
    assert len(commits) == 1 and commits[0]["timestamp"] > second_start
    assert commits[0]["payload"]["text"] == "Please book it for Wednesday afternoon."
    assert not [e for e in of(ev, "playback_started") if e["timestamp"] < second_start]
    turn_ids = {e["turn_id"] for e in ev
                if e["event_type"] in ("speech_start", "speech_end", "endpoint_committed",
                                       "context_committed")}
    assert turn_ids == {1}


def test_empty_llm_reply_ends_turn_without_playback(tmp_path):
    r = run_scenario(scenario("empty", [Speech("Book a table for two please.")], reply="",
                              close_after=("context_committed", 1, 100)), tmp_path)
    assert not of(r.events, "playback_started")
    committed = of(r.events, "context_committed")
    assert [c["payload"]["role"] for c in committed] == ["user"]
    assert r.state_at_close is SessionState.LISTENING
    assert list(r.session.context) == [{"role": "user", "content": "Book a table for two please."}]


def test_asr_stall_overflows_asr_in_without_deadlock(tmp_path):
    words = " ".join(["hello"] * 30)  # 30 × 100ms = 3s
    r = run_scenario(scenario("asr_stall", [Speech(words)], asr_kwargs={"hang_at_ms": 0},
                              close_after=("speech_end", 1, 200)), tmp_path)
    overflows = of(r.events, "queue_overflow", queue="asr_in")
    assert len(overflows) >= 1
    assert overflows[-1]["payload"]["depth"] == 50
    assert r.session.queues["asr_in"].max_depth == 50
    assert all(o["payload"]["dropped"] == 1 for o in overflows)  # 本次 put 丢掉的帧数
    assert [o["payload"]["dropped_total"] for o in overflows] == list(range(1, len(overflows) + 1))
    assert r.reason is CloseReason.EXPLICIT_CLOSE
    assert len(of(r.events, "session_closed")) == 1


def test_first_chunk_timeout_fails_generation_and_returns_to_listening(tmp_path):
    hang = StreamFaults(300, 50, hang_after=0)
    r = run_scenario(scenario("first_timeout", [Speech("Book a table for two please.")],
                              llm=hang, close_after=("generation_failed", 1, 100)), tmp_path)
    ev = r.events
    commit = of(ev, "endpoint_committed")[0]
    to = of(ev, "provider_timeout", provider="llm", first=True)[0]
    assert to["timestamp"] - commit["timestamp"] == 2000
    seqs = [of(ev, t)[0]["sequence_number"]
            for t in ("provider_timeout", "playback_stopped", "generation_cancelled",
                      "stale_event_dropped", "generation_failed", "context_committed")]
    assert seqs == sorted(seqs)
    assert of(ev, "playback_stopped")[0]["payload"]["reason"] == "generation_failed"
    assert of(ev, "generation_cancelled")[0]["payload"]["reason"] == "first_chunk_timeout"
    assert of(ev, "generation_failed")[0]["payload"]["reason"] == "first_chunk_timeout"
    assert of(ev, "stale_event_dropped", gate="purge", phase="purge")
    assert not of(ev, "playback_started")
    assert [c["payload"]["role"] for c in of(ev, "context_committed")] == ["user"]
    assert r.state_at_close is SessionState.LISTENING
    assert r.session.active_generation_id is None


def test_context_is_bounded(tmp_path):
    segs = []
    for i in range(25):
        segs += [Speech("Please book a table."), AfterEvent("playback_stopped", 200, nth=i + 1)]
    r = run_scenario(scenario("context", segs, reply="Sure.",
                              close_after=("context_committed", 50, 100)), tmp_path)
    assert len(of(r.events, "endpoint_committed")) == 25
    assert len(r.session.context) == 20
    assert r.session.context[-1] == {"role": "assistant", "content": "Sure."}
    assert len(r.session._gens) <= 2 and len(r.session._sups) <= 2  # 结束的 generation 被清理


def test_turn_id_and_generation_id_come_from_data(tmp_path):
    segs = [Speech("Book a table."), AfterEvent("playback_stopped", 200),
            Speech("Thanks a lot.")]
    replies = {"Book a table.": "Sure thing.", "Thanks a lot.": "You are welcome."}
    r = run_scenario(scenario("ids", segs, replies=replies,
                              close_after=("context_committed", 4, 100)), tmp_path)
    ev = r.events
    assert [(e["turn_id"], e["generation_id"]) for e in of(ev, "endpoint_committed")] == \
        [(1, None), (2, None)]
    chunks = of(ev, "llm_chunk")
    assert {e["generation_id"] for e in chunks} == {1, 2}
    for e in chunks + of(ev, "tts_chunk") + of(ev, "audio_enqueued") + of(ev, "playback_started"):
        assert e["turn_id"] == e["generation_id"]  # 第 k 轮启动第 k 个 generation
    text = {g: "".join(e["payload"]["text"] for e in chunks if e["generation_id"] == g)
            for g in (1, 2)}
    assert text == {1: "Sure thing. ", 2: "You are welcome. "}
    assert [c["payload"]["text"] for c in of(ev, "context_committed")] == \
        ["Book a table.", "Sure thing.", "Thanks a lot.", "You are welcome."]


def test_utterance_spanning_turn_end_gets_its_own_turn(tmp_path):
    """播放期间开口、轮次结束后才说完的话：成为下一轮的输入，不泄漏进再下一轮。"""
    ten = "one two three four five six seven eight nine ten."
    segs = [Speech("Book a table."), AfterEvent("playback_started", 100), Speech(ten),
            Silence(3000), Speech("Thanks a lot.")]
    r = run_scenario(scenario("spanning", segs, reply="Sure thing, done.",
                              close_after=("context_committed", 6, 100)), tmp_path)
    ev = r.events
    commits = of(ev, "endpoint_committed")
    assert [(c["turn_id"], c["payload"]["text"]) for c in commits] == \
        [(1, "Book a table."), (2, ten), (3, "Thanks a lot.")]
    # 10 词那段话的判停不靠 max_silence 兜底
    assert all(c["payload"]["reason"] == "score_threshold" for c in commits)
    turn1_end = of(ev, "context_committed", role="user")[0]["timestamp"]
    ten_start = of(ev, "script_marker", segment="speech", phase="start", index=2)[0]["timestamp"]
    assert ten_start < turn1_end  # 确实跨越了轮次结束


def test_invalidate_is_idempotent(tmp_path):
    sc = scenario("invalidate", [Speech("Book a table for two please.")])
    log = EventLog(tmp_path / "inv.jsonl", "s1")
    events: list[dict] = []
    log.add_listener(events.append)
    session, _ = build_session(sc, RuntimeConfig(), log)

    async def main():
        task = asyncio.create_task(session.run(), name="test/session")
        while session.active_generation_id is None:
            await asyncio.sleep(0.02)
        gid = session.active_generation_id
        gen, sup = session._gens[gid], session._sups[gid]
        calls = []
        orig = sup.signal_cancel
        sup.signal_cancel = lambda: (calls.append(1), orig())
        session._player.stop_generation(gid, "test")
        session.invalidate(gen, "test")
        session.invalidate(gen, "test")
        assert len(calls) == 1
        session.close()
        session.close()
        return await task

    try:
        assert run(main()) is CloseReason.EXPLICIT_CLOSE
    finally:
        log.close()
    cancelled = of(events, "generation_cancelled")
    assert [c["payload"]["duplicate"] for c in cancelled] == [False, True]
    assert len(of(events, "stale_event_dropped", gate="purge")) == 1
    assert len(of(events, "session_closed")) == 1


def test_l2_scorer_is_wired_and_receives_audio(tmp_path):
    from voice_runtime.providers.base import TurnScore
    from voice_runtime.providers.fake import ScriptedTurnScorer

    got: list[bytes | None] = []

    class SpyL2(ScriptedTurnScorer):
        async def score(self, text, audio=None):
            got.append(audio)
            return await super().score(text, audio)

    r = run_scenario(scenario("l2wired", [Speech("Book a table for two please.")]), tmp_path,
                     l1=ScriptedTurnScorer({}, TurnScore(0.6, 0.4)),
                     l2=SpyL2({}, TurnScore(0.95, 0.9)))
    assert got and got[0]
    ep = of(r.events, "endpoint_committed")[0]["payload"]
    assert ep["level"] == 2
