"""设计 §12 的场景断言。界值都由当前配置推算，推算方法写在断言旁边。"""
from dataclasses import replace

import pytest
from helpers import run_scenario

from voice_runtime.audio import word_duration_ms
from voice_runtime.config import RuntimeConfig
from voice_runtime.queues import BoundedQueue
from voice_runtime.scenarios import (LONG_REPLY, STOP, scenario_a, scenario_a_prime,
                                     scenario_b, scenario_c, scenario_d, scenario_e)

CFG = RuntimeConfig()


def of(events, event_type, **match):
    return [e for e in events if e["event_type"] == event_type
            and all(e["payload"].get(k) == v for k, v in match.items())]


def marker(events, segment, phase, nth=0):
    return of(events, "script_marker", segment=segment, phase=phase)[nth]["timestamp"]


def test_long_reply_is_about_four_seconds():
    for sc in (scenario_c(), scenario_e()):
        assert sc.default_reply == LONG_REPLY
        assert 3600 <= sum(word_duration_ms(w) for w in sc.default_reply.split()) <= 4400


def test_scenario_a_keeps_speculation_and_a_prime_disables_it():
    assert scenario_a_prime().cfg_overrides["speculation_enabled"] is False
    assert scenario_a().cfg_overrides.get("speculation_enabled", True) is True
    assert scenario_a().segments == scenario_a_prime().segments


def test_A_prime_no_extra_queueing(tmp_path):
    sc = scenario_a_prime()
    r = run_scenario(sc, tmp_path)
    # LLM 首包 + 最大抖动 + TTS 首包 + 最大抖动 + 2 个 tick（40ms）
    bound = (sc.llm_faults.first_delay_ms + sc.llm_faults.jitter_ms
             + sc.tts_faults.first_delay_ms + sc.tts_faults.jitter_ms + 40)
    assert bound == 300 + 20 + 200 + 20 + 40
    assert len(r.metrics["turn_end_to_first_audio_ms"]) == 1
    assert r.metrics["turn_end_to_first_audio_ms"][0] <= bound
    assert of(r.events, "playback_stopped")[-1]["payload"]["reason"] == "completed"


def test_B_no_early_playback_and_timely_endpoint(tmp_path):
    r = run_scenario(scenario_b(), tmp_path)
    ev = r.events
    pause_start = marker(ev, "silence", "start")
    resume = marker(ev, "speech", "start", nth=1)
    for t in ("endpoint_committed", "playback_started"):
        assert not [e for e in of(ev, t) if pause_start <= e["timestamp"] <= resume]
    # initial_silence_ms + final_wait_ms + L1 预算
    bound = CFG.initial_silence_ms + CFG.final_wait_ms + CFG.l1_budget_ms
    assert bound == 200 + 300 + 50
    assert len(r.metrics["endpoint_latency_ms"]) == 1
    assert r.metrics["endpoint_latency_ms"][0] <= bound
    assert of(ev, "playback_stopped")[-1]["payload"]["reason"] == "completed"


def test_C_interrupt_under_250ms(tmp_path):
    r = run_scenario(scenario_c(), tmp_path)
    ev, m = r.events, r.metrics
    assert len(m["playback_stop_ms"]) == 1 and m["playback_stop_ms"][0] <= 250
    assert m["interruption_decision_ms"] and m["overlap_ms"][0] <= m["playback_stop_ms"][0]
    stop = of(ev, "playback_stopped", reason="paused_pending_decision")[0]
    cancel = of(ev, "generation_cancelled", reason="barge_in", duplicate=False)
    assert len(cancel) == 1 and cancel[0]["sequence_number"] > stop["sequence_number"]
    purge = of(ev, "stale_event_dropped", gate="purge")
    assert purge[0]["generation_id"] == 1 and purge[0]["payload"]["frames"] > 0
    committed = of(ev, "context_committed")
    old = [c for c in committed if c["turn_id"] == 1 and c["payload"]["role"] == "assistant"][0]
    assert old["payload"]["interrupted"] is True
    heard = old["payload"]["text"]
    assert heard and LONG_REPLY.startswith(heard) and heard != LONG_REPLY
    new_user = [c for c in committed if c["turn_id"] == 2 and c["payload"]["role"] == "user"][0]
    assert new_user["payload"]["text"] == STOP
    assert of(ev, "playback_started")[-1]["generation_id"] == 2
    assert m["stale_chunk_played"] == 0 and m["false_interruption_count"] == 0


def test_D_noise_not_permanent(tmp_path):
    r = run_scenario(scenario_d(), tmp_path)
    ev, m = r.events, r.metrics
    assert m["false_interruption_count"] == 0
    assert m["false_pause_count"] == 1 and m["pause_total_ms"] > 0
    assert m["playback_stop_ms"] == []  # 噪声不是 speech 开口，不计打断延迟
    assert not of(ev, "generation_cancelled", duplicate=False)
    assert [s["payload"]["reason"] for s in of(ev, "playback_stopped")] == \
        ["paused_pending_decision", "completed"]


def _cancel_and_new_first(r):
    ev = r.events
    cancel_ts = of(ev, "generation_cancelled", reason="barge_in", duplicate=False)[0]["timestamp"]
    new_first = next(ts for ts, gid, _ in r.sink.writes if gid == 2)
    return cancel_ts, new_first


def test_E_late_chunks_never_played(tmp_path):
    sc = scenario_e()
    r = run_scenario(sc, tmp_path)
    ev, m = r.events, r.metrics
    cancel_ts, new_first = _cancel_and_new_first(r)
    second_late = sc.tts_faults.late_after_cancel_ms[1]
    grace = sc.cfg_overrides["cancel_grace_ms"]
    assert (second_late, grace) == (3000, 4000)
    # 新首帧相对 cancel 的偏移 < 第 2 个迟到 chunk 的到达偏移 < cancel_grace_ms（设计 §12）
    assert new_first - cancel_ts < second_late < grace
    late = of(ev, "stale_event_dropped", phase="late", unit="chunk")
    assert [x["generation_id"] for x in late] == [1, 1]
    assert late[0]["timestamp"] - cancel_ts == sc.tts_faults.late_after_cancel_ms[0]
    assert late[1]["timestamp"] - cancel_ts == second_late and late[1]["timestamp"] > new_first
    assert m["stale_chunk_received"] == 2 and m["stale_chunk_played"] == 0
    assert not [w for w in r.sink.writes if w[1] == 1 and w[0] >= cancel_ts]
    assert not of(ev, "cancel_grace_exceeded")


def test_E_late_chunk_after_close_never_written(tmp_path):
    sc = replace(scenario_e(), close_after=("bargein_decision", 1, 500))
    r = run_scenario(sc, tmp_path)
    ev = r.events
    close_at = of(ev, "bargein_decision")[0]["timestamp"] + 500
    late = of(ev, "stale_event_dropped", phase="late", unit="chunk")
    assert late and late[-1]["timestamp"] > close_at  # 迟到 chunk 确实落在关闭开始之后
    assert late[-1]["payload"]["gate"] == "session_closed"
    assert all(x["payload"]["gate"] == "session_closed"
               for x in of(ev, "stale_event_dropped") if x["timestamp"] > close_at)
    assert all(ts < close_at for ts, _, _ in r.sink.writes)
    assert r.sink.closed
    closed = of(ev, "session_closed")[0]
    assert closed["sequence_number"] > late[-1]["sequence_number"]
    assert r.metrics["stale_chunk_played"] == 0


@pytest.mark.parametrize("make", [scenario_a_prime, scenario_b, scenario_c, scenario_d,
                                  scenario_e])
def test_all_queues_bounded(tmp_path, make):
    r = run_scenario(make(), tmp_path)
    cfg = r.session.cfg
    caps = {"asr_in": cfg.asr_in_capacity, "playback": cfg.playback_capacity_frames,
            "text": cfg.text_capacity}
    qmd = r.metrics["queue_max_depth"]
    assert set(qmd) == set(caps)
    assert all(0 <= qmd[k] <= caps[k] for k in caps)
    assert all(q.capacity > 0 for q in r.session.queues.values())
    with pytest.raises(ValueError):
        BoundedQueue("x", 0)


def test_trace_is_byte_identical_across_runs(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    run_scenario(scenario_c(), a)
    run_scenario(scenario_c(), b)
    data = (a / "C.jsonl").read_bytes()
    assert data and data == (b / "C.jsonl").read_bytes()
