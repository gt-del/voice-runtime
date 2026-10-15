from voice_runtime.metrics import compute_metrics

KEYS = {"endpoint_latency_ms", "turn_end_to_first_audio_ms", "speech_start_detection_ms",
        "playback_stop_ms", "interruption_decision_ms", "overlap_ms", "false_interruption_count",
        "false_pause_count", "pause_total_ms", "stale_chunk_received", "stale_chunk_played",
        "stale_purged_frames", "stale_rejected_on_wake", "queue_max_depth"}


class Log:
    def __init__(self):
        self.events: list[dict] = []

    def __call__(self, t, event_type, gen=None, turn=None, **payload):
        self.events.append({"timestamp": t, "session_id": "s", "turn_id": turn,
                            "generation_id": gen, "event_type": event_type,
                            "sequence_number": len(self.events), "payload": payload})
        return self

    def marker(self, t, segment, phase):
        return self(t, "script_marker", eval_only=True, segment=segment, phase=phase, index=0,
                    text="")


def test_empty_log_has_all_keys():
    m = compute_metrics([])
    assert set(m) == KEYS
    assert m["endpoint_latency_ms"] == [] and m["queue_max_depth"] == {}
    assert m["false_pause_count"] == 0 and m["stale_chunk_played"] == 0


def test_endpoint_and_first_audio_latency():
    e = Log()
    e.marker(0, "speech", "start").marker(1000, "speech", "end")
    e(1400, "endpoint_committed", turn=1)
    e(1400, "playback_started", gen=0)  # 不可能出现，但 generation_id 不大于之前的值时不计
    e.marker(1500, "silence", "end")  # 非 speech 的 end 不作为说完时刻
    e(1900, "playback_started", gen=1, turn=1)
    e(1950, "playback_started", gen=1, turn=1)
    e.marker(3000, "speech", "start").marker(3500, "speech", "end")
    e(3900, "endpoint_committed", turn=2)
    e(4500, "playback_started", gen=2, turn=None)  # turn_id 为 None 也能处理
    m = compute_metrics(e.events)
    assert m["endpoint_latency_ms"] == [400, 400]
    assert m["turn_end_to_first_audio_ms"] == [500, 600]


def test_endpoint_without_playback_is_skipped():
    e = Log()
    e.marker(0, "speech", "start").marker(500, "speech", "end")
    e(900, "endpoint_committed", turn=1)
    m = compute_metrics(e.events)
    assert m["endpoint_latency_ms"] == [400] and m["turn_end_to_first_audio_ms"] == []


def test_bargein_latencies_and_overlap():
    e = Log()
    e(1000, "playback_started", gen=1)
    e.marker(2000, "speech", "start")
    e(2060, "speech_start", ts_ms=2000)
    e(2080, "playback_stopped", gen=1, reason="paused_pending_decision")
    e(2300, "bargein_decision", gen=1, result="interrupt")
    e(2300, "playback_stopped", gen=1, reason="interrupted")
    e.marker(2500, "speech", "end")
    m = compute_metrics(e.events)
    assert m["speech_start_detection_ms"] == [60]
    assert m["playback_stop_ms"] == [80]
    assert m["interruption_decision_ms"] == [300]
    assert m["overlap_ms"] == [80]
    assert m["false_interruption_count"] == 0


def test_overlap_ends_at_user_end_when_earlier_and_no_decision_omitted():
    e = Log()
    e(0, "playback_started", gen=1)
    e.marker(1000, "speech", "start")
    e.marker(1040, "speech", "end")  # 40ms 的短语音，停播之前就结束
    e(1060, "speech_start")
    e(1080, "playback_stopped", gen=1, reason="paused_pending_decision")
    e(1400, "bargein_decision", gen=1, result="backchannel")
    e(1400, "playback_resumed", gen=1, paused_ms=320)
    m = compute_metrics(e.events)
    assert m["overlap_ms"] == [40]
    assert m["interruption_decision_ms"] == []
    assert m["false_pause_count"] == 1 and m["pause_total_ms"] == 320


def test_speech_outside_playback_is_not_an_onset():
    e = Log()
    e.marker(0, "speech", "start")  # 没有声音在播
    e(60, "speech_start")
    e(100, "playback_started", gen=1)
    e(900, "playback_stopped", gen=1, reason="completed")
    e.marker(1000, "speech", "start")  # 已播完
    e(1060, "speech_start")
    m = compute_metrics(e.events)
    assert m["speech_start_detection_ms"] == [] and m["overlap_ms"] == []


def test_playback_stop_pairs_only_with_its_own_segment():
    e = Log()
    e(0, "playback_started", gen=1)
    e.marker(500, "speech", "start")  # 这段话没有触发暂停（例如太短）
    e.marker(540, "speech", "end")
    e(560, "speech_start")
    e.marker(1000, "noise", "start")  # 之后的噪声才触发暂停：不属于上面那个开口
    e(1060, "playback_stopped", gen=1, reason="paused_pending_decision")
    m = compute_metrics(e.events)
    assert m["playback_stop_ms"] == []
    assert m["overlap_ms"] == [40]


def test_onset_while_paused_has_zero_overlap():
    e = Log()
    e(0, "playback_started", gen=1)
    e.marker(500, "noise", "start")
    e(560, "playback_stopped", gen=1, reason="paused_pending_decision")
    e.marker(580, "noise", "end")
    e.marker(600, "speech", "start")  # 暂停期间开口：没有声音在播
    e(700, "speech_start")
    m = compute_metrics(e.events)
    assert m["overlap_ms"] == [0]
    assert m["playback_stop_ms"] == []


def test_false_interruption_counts_interrupts_caused_by_noise():
    e = Log()
    e(0, "playback_started", gen=1)
    e.marker(500, "noise", "start").marker(580, "noise", "end")
    e(990, "bargein_decision", gen=1, result="interrupt")  # 噪声之后、没有语音：误打断
    e(1000, "playback_started", gen=2)
    e.marker(1500, "noise", "start").marker(1580, "noise", "end")
    e.marker(1600, "speech", "start")
    e(1700, "bargein_decision", gen=2, result="interrupt")  # 中间有语音：真打断
    e.marker(2000, "noise", "start")
    e(2100, "bargein_decision", gen=3, result="noise")  # 判为噪声：不算
    m = compute_metrics(e.events)
    assert m["false_interruption_count"] == 1


def test_stale_counters_are_separate():
    e = Log()
    e(10, "stale_event_dropped", gen=1, gate="2", phase="late", unit="chunk")
    e(20, "stale_event_dropped", gen=1, gate="2", phase="late", unit="chunk")
    e(20, "stale_event_dropped", gen=1, gate="1", phase="late", unit="text")
    e(20, "stale_event_dropped", gen=1, gate="3", phase="late", unit="frame")
    e(30, "stale_event_dropped", gen=1, gate="2", phase="rejected_on_wake", unit="chunk")
    e(30, "stale_event_dropped", gen=1, gate="2", phase="rejected_on_wake", unit="eos")
    e(40, "stale_event_dropped", gen=1, gate="purge", phase="purge", unit="frame", frames=7)
    e(50, "stale_event_dropped", gen=2, gate="purge", phase="purge", unit="frame", frames=3)
    m = compute_metrics(e.events)
    assert m["stale_chunk_received"] == 2
    assert m["stale_purged_frames"] == 10
    assert m["stale_rejected_on_wake"] == 2


def test_stale_chunk_played_uses_sequence_order():
    e = Log()
    e(100, "playback_progress", gen=1, ranges=[[1, 0, 4]])  # 取消之前：不算
    e(200, "generation_cancelled", gen=1, reason="barge_in", duplicate=False)
    e(200, "playback_progress", gen=1, ranges=[[1, 5, 6]])  # 同一时刻但序号在后：算 2 帧
    e(300, "generation_cancelled", gen=2, reason="x", duplicate=True)  # duplicate 不算
    e(400, "playback_progress", gen=2, ranges=[[2, 0, 9]])
    m = compute_metrics(e.events)
    assert m["stale_chunk_played"] == 2


def test_queue_max_depth_from_session_closed():
    e = Log()
    e(0, "session_closed", reason="explicit_close", residual={"asr_in": 0},
      queue_max_depth={"asr_in": 3, "playback": 75}, overdue_task_count=0)
    assert compute_metrics(e.events)["queue_max_depth"] == {"asr_in": 3, "playback": 75}
