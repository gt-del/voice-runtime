import asyncio

import pytest

from voice_runtime.config import RuntimeConfig
from voice_runtime.endpointing.cascade import EndpointCascade, high_threshold
from voice_runtime.endpointing.rules import RuleTurnScorer
from voice_runtime.events import (PROVIDER_TIMEOUT, STALE_EVENT_DROPPED, TASK_FAILED, TURN_SCORE,
                                  EventLog)
from voice_runtime.providers.base import TurnScore
from voice_runtime.providers.fake import ScriptedTurnScorer
from voice_runtime.speech import SpeechEdge
from voice_runtime.vloop import now_ms, run

END_TS = 1000  # 回溯后的 speech_end 时间戳；VAD 在 80ms 后（1080）才报出边沿


def _spawn(coro, name):
    return asyncio.get_running_loop().create_task(coro, name=name)


def _mk(tmp_path, scorer):
    log = EventLog(tmp_path / "e.jsonl", "s")
    events: list[dict] = []
    log.add_listener(events.append)
    return EndpointCascade(RuntimeConfig(), log, [scorer], spawn=_spawn), events


def _of(events, kind):
    return [e for e in events if e["event_type"] == kind]


async def _sleep_to(t_ms):
    await asyncio.sleep((t_ms - now_ms()) / 1000)


async def _speak_then_end(c, text, is_final=True):
    """t=0 开口，说出 text，t=1080 报出回溯到 1000 的 speech_end。"""
    c.on_speech_edge(SpeechEdge("start", 0, 1))
    await _sleep_to(500)
    c.on_text(text, is_final)
    await _sleep_to(END_TS + 80)
    c.on_speech_edge(SpeechEdge("end", END_TS, 1))


async def _tick_until(c, until_ms):
    """每 20ms tick 一次，返回第一个判停结果；到 until_ms 仍未判停则返回 None。"""
    while now_ms() <= until_ms:
        d = c.tick(now_ms())
        if d is not None:
            return d
        await asyncio.sleep(0.02)
    return None


@pytest.mark.parametrize("t,exp", [(199, None), (200, 0.85), (600, 0.675), (1000, 0.5), (1400, 0.5)])
def test_high_threshold_curve(t, exp):
    got = high_threshold(t, RuntimeConfig())
    if exp is None:
        assert got is None
    else:
        assert got == pytest.approx(exp)


def test_complete_text_commits_at_initial_silence(tmp_path):
    async def body():
        c, events = _mk(tmp_path, RuleTurnScorer())
        await _speak_then_end(c, "Please book it for Wednesday afternoon.")
        return await _tick_until(c, END_TS + 2000), events
    d, events = run(body())
    assert d.reason == "score_threshold"
    assert d.decided_at_ms == END_TS + 200
    assert d.threshold == pytest.approx(0.85)
    assert d.level == 1 and d.p == pytest.approx(0.95)
    ts = _of(events, TURN_SCORE)
    assert len(ts) == 1 and ts[0]["payload"]["decision"] == "commit_candidate"
    assert ts[0]["payload"]["rules"] == ["terminal_punct"]


def test_incomplete_text_never_commits_before_max_silence(tmp_path):
    async def body():
        c, events = _mk(tmp_path, RuleTurnScorer())
        await _speak_then_end(c, "Please book it for")
        before = await _tick_until(c, END_TS + 700)
        after = await _tick_until(c, END_TS + 2000)
        return before, after, events
    before, after, events = run(body())
    assert before is None
    assert after.reason == "max_silence"
    assert after.decided_at_ms == END_TS + 1500
    assert _of(events, TURN_SCORE)[0]["payload"]["decision"] == "incomplete"


def test_mid_score_commits_when_threshold_decays(tmp_path):
    async def body():
        c, events = _mk(tmp_path, ScriptedTurnScorer({}, TurnScore(0.6, 0.9)))
        await _speak_then_end(c, "book it")
        return await _tick_until(c, END_TS + 2000), events
    d, events = run(body())
    assert d.reason == "score_threshold"
    assert d.decided_at_ms - END_TS == 780  # 理论值约 771ms，对齐到 20ms tick
    assert d.threshold <= 0.6
    assert _of(events, TURN_SCORE)[0]["payload"]["decision"] == "uncertain"


def test_low_confidence_does_not_commit_even_if_p_high(tmp_path):
    async def body():
        c, _ = _mk(tmp_path, ScriptedTurnScorer({}, TurnScore(0.9, 0.3)))
        await _speak_then_end(c, "book it")
        return await _tick_until(c, END_TS + 2000)
    d = run(body())
    assert d.reason == "max_silence"
    assert d.decided_at_ms == END_TS + 1500


def test_speech_start_invalidates_inflight_score_as_stale(tmp_path):
    async def body():
        c, events = _mk(tmp_path, ScriptedTurnScorer({}, TurnScore(0.95, 0.9), delay_ms=50))
        await _speak_then_end(c, "book it for wednesday")
        await _sleep_to(END_TS + 100)
        c.on_speech_edge(SpeechEdge("start", END_TS + 40, 2))
        await _sleep_to(END_TS + 300)
        return c, events
    c, events = run(body())
    stale = _of(events, STALE_EVENT_DROPPED)
    assert len(stale) == 1
    assert stale[0]["payload"]["gate"] == "endpoint"
    assert stale[0]["payload"]["phase"] == "late"
    assert stale[0]["payload"]["level"] == 1
    assert stale[0]["payload"]["epoch"] == 1
    assert _of(events, TURN_SCORE) == []
    assert c.latest_l1 is None


def test_final_rewrites_text_and_old_score_is_stale(tmp_path):
    final = "Book it for Wednesday afternoon."
    scorer = ScriptedTurnScorer({final: TurnScore(0.95, 0.8)}, TurnScore(0.6, 0.4), delay_ms=40)

    async def body():
        c, events = _mk(tmp_path, scorer)
        await _speak_then_end(c, "book it for wednesday", is_final=False)
        old_version = c.text_version
        await _sleep_to(END_TS + 100)  # 旧打分（1080 发起、40ms）在途时 final 到达
        c.on_text(final, True)
        d = await _tick_until(c, END_TS + 2000)
        return c, d, events, old_version
    c, d, events, old_version = run(body())
    stale = _of(events, STALE_EVENT_DROPPED)
    assert [s["payload"]["text_version"] for s in stale] == [old_version]
    ts = _of(events, TURN_SCORE)
    assert len(ts) == 1 and ts[0]["payload"]["text_version"] == c.text_version != old_version
    assert d.reason == "score_threshold" and d.decided_at_ms == END_TS + 200
    assert c.current_text == final and c.have_final_for_last_segment


def test_scorer_exception_is_uncertain_not_fatal(tmp_path):
    async def body():
        c, events = _mk(tmp_path, ScriptedTurnScorer({}, TurnScore(0.95, 0.9), raise_exc=True))
        await _speak_then_end(c, "book it")
        return await _tick_until(c, END_TS + 2000), events
    d, events = run(body())
    failed = _of(events, TASK_FAILED)
    assert len(failed) == 1
    assert failed[0]["payload"] == {"level": 1, "task": "session/score-l1",
                                     "error": "RuntimeError: fake failure"}
    assert _of(events, TURN_SCORE)[0]["payload"]["decision"] == "uncertain"
    assert d.reason == "max_silence"


def test_scorer_over_budget_is_timeout_and_uncertain(tmp_path):
    async def body():
        c, events = _mk(tmp_path, ScriptedTurnScorer({}, TurnScore(0.95, 0.9), hang=True))
        await _speak_then_end(c, "book it")
        return await _tick_until(c, END_TS + 2000), events
    d, events = run(body())
    to = _of(events, PROVIDER_TIMEOUT)
    assert len(to) == 1 and to[0]["payload"]["level"] == 1
    assert to[0]["timestamp"] == END_TS + 80 + 50
    assert d.reason == "max_silence"


def test_text_change_in_silence_rescores_and_callback_fires(tmp_path):
    scorer = ScriptedTurnScorer({"book it for wednesday": TurnScore(0.95, 0.9)}, TurnScore(0.1, 0.9))
    seen = []

    async def body():
        c, events = _mk(tmp_path, scorer)
        c.turn_id = 7
        c.on_l1_scored = lambda s, t: seen.append((s.p_complete, t))
        await _speak_then_end(c, "book it for", is_final=True)
        await _sleep_to(END_TS + 100)
        c.on_text("wednesday", False)
        await _sleep_to(END_TS + 120)
        c.on_text("wednesday", False)  # 文本没变，不重复打分
        await _sleep_to(END_TS + 150)
        return c, events
    c, events = run(body())
    assert c.current_text == "book it for wednesday"
    assert not c.have_final_for_last_segment
    assert seen == [(0.1, "book it for"), (0.95, "book it for wednesday")]
    assert c.latest_l1 == TurnScore(0.95, 0.9)
    assert [e["turn_id"] for e in _of(events, TURN_SCORE)] == [7, 7]


def test_partial_during_speech_is_not_scored(tmp_path):
    async def body():
        c, events = _mk(tmp_path, RuleTurnScorer())
        c.on_speech_edge(SpeechEdge("start", 0, 1))
        c.on_text("book it", False)
        await _sleep_to(200)
        return events
    assert _of(run(body()), TURN_SCORE) == []


def test_empty_text_never_decides(tmp_path):
    async def body():
        c, events = _mk(tmp_path, RuleTurnScorer())
        c.on_speech_edge(SpeechEdge("start", 0, 1))
        await _sleep_to(END_TS + 80)
        c.on_speech_edge(SpeechEdge("end", END_TS, 1))
        return await _tick_until(c, END_TS + 3000), events
    d, events = run(body())
    assert d is None
    assert _of(events, TURN_SCORE) == []


def test_decision_fires_once_until_reset_or_speech_start(tmp_path):
    async def body():
        c, _ = _mk(tmp_path, RuleTurnScorer())
        await _speak_then_end(c, "Book it for Wednesday afternoon.")
        first = await _tick_until(c, END_TS + 2000)
        again = await _tick_until(c, END_TS + 400)
        c.on_speech_edge(SpeechEdge("start", now_ms(), 2))
        c.on_text("Thanks.", True)
        c.on_speech_edge(SpeechEdge("end", now_ms(), 2))
        third = await _tick_until(c, now_ms() + 2000)
        c.reset()
        return c, first, again, third
    c, first, again, third = run(body())
    assert first is not None and again is None and third is not None
    assert c.current_text == "" and c.latest_l1 is None


def test_reset_with_inflight_score_logs_stale_under_old_turn(tmp_path):
    async def body():
        c, events = _mk(tmp_path, ScriptedTurnScorer({}, TurnScore(0.95, 0.9), delay_ms=40))
        c.turn_id = 1
        await _speak_then_end(c, "Book it for Wednesday afternoon.")
        await _sleep_to(END_TS + 100)
        c.reset()
        c.turn_id = 2
        await _sleep_to(END_TS + 200)
        return c, events
    c, events = run(body())
    stale = _of(events, STALE_EVENT_DROPPED)
    assert len(stale) == 1 and stale[0]["turn_id"] == 1
    assert _of(events, TURN_SCORE) == []
    assert c.latest_l1 is None


def test_reset_clears_silence_so_no_decision_without_new_speech_end(tmp_path):
    async def body():
        c, _ = _mk(tmp_path, RuleTurnScorer())
        await _speak_then_end(c, "Please book it for")
        c.reset()
        c.on_text("Book it for Wednesday afternoon.", True)
        return await _tick_until(c, END_TS + 3000)
    assert run(body()) is None


def test_empty_final_adds_nothing(tmp_path):
    c, _ = _mk(tmp_path, RuleTurnScorer())
    c.on_text("book it", True)
    version = c.text_version
    c.on_text("", True)
    c.on_text("now", True)
    assert c.current_text == "book it now"
    assert c.text_version == version + 1


def test_on_l1_scored_exception_is_logged_not_fatal(tmp_path):
    async def body():
        c, events = _mk(tmp_path, RuleTurnScorer())

        def boom(s, t):
            raise ValueError("bad")
        c.on_l1_scored = boom
        await _speak_then_end(c, "Book it for Wednesday afternoon.")
        return await _tick_until(c, END_TS + 2000), events
    d, events = run(body())
    failed = _of(events, TASK_FAILED)
    assert [f["payload"] for f in failed] == [{"task": "session/score-l1", "error": "ValueError: bad"}]
    assert d.reason == "score_threshold" and d.decided_at_ms == END_TS + 200


def test_speech_start_clears_have_final_for_last_segment(tmp_path):
    """新语音段开口后，上一段的 final 不能再满足"等 final"。"""
    async def main():
        c, _ = _mk(tmp_path, RuleTurnScorer())
        c.on_speech_edge(SpeechEdge("start", 0, 1))
        c.on_text("Please book it for", True)
        assert c.have_final_for_last_segment
        c.on_speech_edge(SpeechEdge("start", 1700, 2))
        assert not c.have_final_for_last_segment
        c._log.close()

    run(main())


def _mk3(tmp_path, *scorers):
    log = EventLog(tmp_path / "e.jsonl", "s")
    events: list[dict] = []
    log.add_listener(events.append)
    return EndpointCascade(RuntimeConfig(), log, list(scorers), spawn=_spawn), events


class _Spy(ScriptedTurnScorer):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls: list[tuple[int, bytes | None]] = []

    async def score(self, text, audio=None):
        self.calls.append((now_ms(), audio))
        return await super().score(text, audio)


def test_uncertain_l1_escalates_immediately_at_speech_end(tmp_path):
    async def body():
        l2 = _Spy({}, TurnScore(0.95, 0.9))
        c, events = _mk3(tmp_path, ScriptedTurnScorer({}, TurnScore(0.6, 0.4)), l2)
        c.feed_audio(b"\x01" * 640)
        await _speak_then_end(c, "book it")
        await _sleep_to(END_TS + 90)
        return l2, events
    l2, events = run(body())
    assert [t for t, _ in l2.calls] == [END_TS + 80]  # 与 speech_end 同一 tick，不等 200ms
    assert l2.calls[0][1] == b"\x01" * 640


def test_l2_result_overrides_l1_for_decision(tmp_path):
    async def body():
        c, events = _mk3(tmp_path, ScriptedTurnScorer({}, TurnScore(0.6, 0.4)),
                         ScriptedTurnScorer({}, TurnScore(0.95, 0.9)))
        await _speak_then_end(c, "book it")
        return await _tick_until(c, END_TS + 2000), events
    d, events = run(body())
    assert d.reason == "score_threshold" and d.level == 2
    assert d.decided_at_ms == END_TS + 200
    assert [e["payload"]["level"] for e in _of(events, TURN_SCORE)] == [1, 2]


def test_at_most_one_inflight_per_level(tmp_path):
    async def body():
        l2 = _Spy({}, TurnScore(0.5, 0.9), delay_ms=100)
        c, _ = _mk3(tmp_path, ScriptedTurnScorer({}, TurnScore(0.6, 0.4)), l2)
        await _speak_then_end(c, "book it")
        await _sleep_to(END_TS + 90)
        c.on_text("", False)  # 文本没变，不重复请求
        await _sleep_to(END_TS + 280)
        return l2
    assert len(run(body()).calls) == 1


def test_incomplete_does_not_escalate(tmp_path):
    async def body():
        l2 = _Spy({}, TurnScore(0.95, 0.9))
        c, _ = _mk3(tmp_path, ScriptedTurnScorer({}, TurnScore(0.1, 0.9)), l2)
        await _speak_then_end(c, "book it for")
        await _sleep_to(END_TS + 300)
        return l2
    assert run(body()).calls == []


def test_l3_timeout_falls_back_to_threshold_curve(tmp_path):
    async def body():
        c, events = _mk3(tmp_path, ScriptedTurnScorer({}, TurnScore(0.6, 0.9)),
                         ScriptedTurnScorer({}, TurnScore(0.6, 0.4), raise_exc=True),
                         ScriptedTurnScorer({}, TurnScore(0.99, 0.9), hang=True))
        await _speak_then_end(c, "book it")
        return await _tick_until(c, END_TS + 2000), events
    d, events = run(body())
    to = _of(events, PROVIDER_TIMEOUT)
    assert len(to) == 1 and to[0]["payload"]["level"] == 3
    assert to[0]["timestamp"] == END_TS + 80 + 500  # L2 失败后 L3 立即发起，500ms 预算
    assert d.reason == "score_threshold" and d.level == 1  # 超时的 L3 不覆盖 L1 的真实分
    assert d.decided_at_ms - END_TS == 780


def test_audio_ring_is_bounded(tmp_path):
    async def body():
        c, _ = _mk3(tmp_path, RuleTurnScorer())
        for i in range(500):  # 10 秒
            c.feed_audio(bytes([i % 256]) * 640)
        return c
    c = run(body())
    assert len(c._ring) == 150
