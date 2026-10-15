from helpers import run_scenario

from voice_runtime.audio import word_duration_ms
from voice_runtime.providers.fake import StreamFaults
from voice_runtime.scenarios import Scenario
from voice_runtime.script import AfterEvent, Noise, Speech

LLM = StreamFaults(300, 50)
TTS = StreamFaults(200, 40)
REQUEST = "Book a table for two please."
LONG_REPLY = ("Sure, I can help with that. Your table for two is booked for Thursday at seven in "
              "the evening, and I have added a note about the quiet window seat you asked for "
              "earlier today, so everything is ready.")
STOP = "Stop. Make it Friday instead."
FRIDAY_REPLY = "OK, Friday it is. I have moved your booking to Friday evening at seven."


def of(events, event_type, **match):
    return [e for e in events if e["event_type"] == event_type
            and all(e["payload"].get(k) == v for k, v in match.items())]


def scenario(name, segments, *, reply=LONG_REPLY, replies=None,
             close_after=("context_committed", 2, 100), **kw):
    return Scenario(name=name, segments=segments, llm_replies=replies or {},
                    default_reply=reply, llm_faults=LLM, tts_faults=TTS,
                    close_after=close_after, **kw)


def onset(events, index):
    return of(events, "script_marker", phase="start", index=index)[0]["timestamp"]


def first_after(events, event_type, t, **match):
    return next(e for e in of(events, event_type, **match) if e["timestamp"] >= t)


def assert_interrupted_turn(ev, gid):
    stop = [s for s in of(ev, "playback_stopped", reason="interrupted") if s["generation_id"] == gid]
    assert len(stop) == 1
    cancel = of(ev, "generation_cancelled", reason="barge_in", duplicate=False)
    cancel = [c for c in cancel if c["generation_id"] == gid]
    assert len(cancel) == 1 and stop[0]["sequence_number"] < cancel[0]["sequence_number"]
    purge = [p for p in of(ev, "stale_event_dropped", gate="purge") if p["generation_id"] == gid]
    assert purge[0]["sequence_number"] > cancel[0]["sequence_number"]
    return cancel[0], purge[0]


def test_command_word_interrupt_stops_within_250ms_and_starts_new_turn(tmp_path):
    assert 3600 <= sum(word_duration_ms(w) for w in LONG_REPLY.split()) <= 4400
    segs = [Speech(REQUEST), AfterEvent("playback_started", 1200), Speech(STOP)]
    r = run_scenario(scenario("command", segs, replies={STOP: FRIDAY_REPLY},
                              close_after=("context_committed", 4, 100)), tmp_path)
    ev = r.events
    t0 = onset(ev, 2)
    paused = first_after(ev, "playback_stopped", t0)
    assert paused["payload"]["reason"] == "paused_pending_decision"
    assert paused["timestamp"] - t0 <= 250
    decision = of(ev, "bargein_decision")
    assert len(decision) == 1
    d = decision[0]
    assert d["payload"]["result"] == "interrupt" and d["payload"]["rule"] == "command_word"
    assert d["payload"]["late_interrupt"] is False and d["payload"]["utterance_id"] == 1
    assert d["turn_id"] == 1 and d["generation_id"] == 1
    cancel, purge = assert_interrupted_turn(ev, 1)
    assert cancel["sequence_number"] > paused["sequence_number"]
    assert purge["payload"]["frames"] > 0
    committed = of(ev, "context_committed")
    old_assistant = [c for c in committed if c["turn_id"] == 1 and c["payload"]["role"] == "assistant"]
    assert old_assistant[0]["payload"]["interrupted"] is True
    heard = old_assistant[0]["payload"]["text"]
    assert heard and LONG_REPLY.startswith(heard) and heard != LONG_REPLY
    new_user = [c for c in committed if c["turn_id"] == 2 and c["payload"]["role"] == "user"]
    assert new_user[0]["payload"]["text"] == STOP
    assert [e["turn_id"] for e in of(ev, "endpoint_committed")] == [1, 2]
    assert of(ev, "playback_started")[-1]["generation_id"] == 2
    assert "Friday" in r.session.context[-1]["content"]


def test_noise_80ms_pauses_then_resumes_and_completes(tmp_path):
    segs = [Speech(REQUEST), AfterEvent("playback_started", 1000), Noise(80)]
    r = run_scenario(scenario("noise", segs), tmp_path)
    ev = r.events
    d = of(ev, "bargein_decision")
    assert [(x["payload"]["result"], x["payload"]["rule"]) for x in d] == \
        [("noise", "noise_empty_final")]
    resumed = of(ev, "playback_resumed")
    assert len(resumed) == 1 and resumed[0]["sequence_number"] > d[0]["sequence_number"]
    assert [s["payload"]["reason"] for s in of(ev, "playback_stopped")] == \
        ["paused_pending_decision", "completed"]
    assert not of(ev, "generation_cancelled")
    idx = [i for _, g, i in r.sink.writes if g == 1]
    assert idx == list(range(len(idx)))
    assert of(ev, "context_committed", role="assistant")[0]["payload"]["text"] == LONG_REPLY


def test_backchannel_resumes_and_transcript_not_in_context(tmp_path):
    segs = [Speech(REQUEST), AfterEvent("playback_started", 1000), Speech("Yeah.")]
    r = run_scenario(scenario("backchannel", segs), tmp_path)
    ev = r.events
    d = of(ev, "bargein_decision")
    assert [x["payload"]["result"] for x in d] == ["backchannel"]
    assert d[0]["payload"]["scores"] and d[0]["payload"]["scorer"] == "ok"
    assert of(ev, "playback_resumed")
    assert of(ev, "playback_stopped")[-1]["payload"]["reason"] == "completed"
    assert all("yeah" not in c["payload"]["text"].lower() for c in of(ev, "context_committed"))
    assert all("yeah" not in m["content"].lower() for m in r.session.context)
    assert len(of(ev, "endpoint_committed")) == 1


def test_i_see_not_interrupted_at_first_word(tmp_path):
    segs = [Speech(REQUEST), AfterEvent("playback_started", 1000), Speech("I see.")]
    r = run_scenario(scenario("i_see", segs), tmp_path)
    ev = r.events
    assert of(ev, "asr_partial", text="I")  # 第一个词确实单独到达过门禁
    assert [x["payload"]["result"] for x in of(ev, "bargein_decision")] == ["backchannel"]
    assert not of(ev, "generation_cancelled")
    assert of(ev, "playback_stopped")[-1]["payload"]["reason"] == "completed"


def test_late_words_after_noise_trigger_late_interrupt(tmp_path):
    segs = [Speech(REQUEST), AfterEvent("playback_started", 1200), Speech("Stop.")]
    asr = {"partial_latency_ms": 600, "final_latency_ms": 700}
    r = run_scenario(scenario("late", segs, asr_kwargs=asr, reply=LONG_REPLY,
                              replies={"Stop.": "OK."},
                              close_after=("context_committed", 4, 100)), tmp_path)
    ev = r.events
    d = of(ev, "bargein_decision")
    assert [(x["payload"]["result"], x["payload"]["rule"], x["payload"]["late_interrupt"])
            for x in d] == [("noise", "noise_timeout", False), ("interrupt", "command_word", True)]
    assert d[0]["payload"]["utterance_id"] == d[1]["payload"]["utterance_id"]
    resumed = of(ev, "playback_resumed")
    assert resumed and resumed[0]["sequence_number"] < d[1]["sequence_number"]
    assert_interrupted_turn(ev, 1)
    assistant = of(ev, "context_committed", role="assistant")[0]
    assert assistant["turn_id"] == 1 and assistant["payload"]["interrupted"] is True
    assert of(ev, "endpoint_committed")[-1]["turn_id"] == 2


def test_user_continues_during_pending_merges_into_same_turn(tmp_path):
    first = "Please book it for Wednesday."
    segs = [Speech(first), AfterEvent("endpoint_committed", 100), Speech("afternoon.")]
    r = run_scenario(scenario("pending_continue", segs, reply="Sure.",
                              close_after=("context_committed", 2, 100)), tmp_path)
    ev = r.events
    assert not [e for e in of(ev, "playback_started") if e["generation_id"] == 1]
    d = of(ev, "bargein_decision")
    assert [x["payload"]["result"] for x in d] == ["interrupt"]
    cancel = of(ev, "generation_cancelled", reason="user_continued")
    assert [c["generation_id"] for c in cancel] == [1]
    commits = of(ev, "endpoint_committed")
    assert [c["turn_id"] for c in commits] == [1, 1]
    assert first in commits[1]["payload"]["text"] and "afternoon" in commits[1]["payload"]["text"]
    user = of(ev, "context_committed", role="user")
    assert len(user) == 1 and user[0]["turn_id"] == 1
    assert "afternoon" in user[0]["payload"]["text"]
    assert of(ev, "playback_started")[0]["generation_id"] == 2


def test_cough_during_pending_keeps_generation(tmp_path):
    segs = [Speech(REQUEST), AfterEvent("endpoint_committed", 100), Noise(80)]
    r = run_scenario(scenario("pending_cough", segs, reply="Sure, booked."), tmp_path)
    ev = r.events
    assert [x["payload"]["result"] for x in of(ev, "bargein_decision")] == ["noise"]
    assert not of(ev, "generation_cancelled")
    assert not of(ev, "playback_resumed")  # pending 中没有声音被暂停
    assert of(ev, "playback_started")[0]["generation_id"] == 1
    assert of(ev, "playback_stopped")[-1]["payload"]["reason"] == "completed"
    assert of(ev, "context_committed", role="assistant")[0]["payload"]["text"] == "Sure, booked."


def test_double_barge_in_handles_both(tmp_path):
    second = "Wait. Make it Saturday."
    segs = [Speech(REQUEST), AfterEvent("playback_started", 1200), Speech(STOP),
            AfterEvent("playback_started", 300, nth=2), Speech(second)]
    replies = {STOP: FRIDAY_REPLY, second: "Done, Saturday works."}
    r = run_scenario(scenario("double", segs, replies=replies,
                              close_after=("context_committed", 6, 100)), tmp_path)
    ev = r.events
    for index in (2, 4):
        t0 = onset(ev, index)
        paused = first_after(ev, "playback_stopped", t0)
        assert paused["payload"]["reason"] == "paused_pending_decision"
        assert paused["timestamp"] - t0 <= 250
    assert [(x["generation_id"], x["payload"]["result"]) for x in of(ev, "bargein_decision")] == \
        [(1, "interrupt"), (2, "interrupt")]
    for gid in (1, 2):
        cancel, _ = assert_interrupted_turn(ev, gid)
        assert not [w for w in r.sink.writes if w[1] == gid and w[0] > cancel["timestamp"]]
    assert [e["turn_id"] for e in of(ev, "endpoint_committed")] == [1, 2, 3]
    assert r.session.context[-1] == {"role": "assistant", "content": "Done, Saturday works."}
