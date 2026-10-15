from voice_runtime.config import RuntimeConfig
from voice_runtime.events import ALL_EVENT_TYPES, EventLog, read_jsonl
from voice_runtime.vloop import run


def _async(fn):
    async def wrapper():
        return fn()
    return wrapper()


def test_emit_writes_required_fields_and_increments_seq(tmp_path):
    def body():
        log = EventLog(tmp_path / "t.jsonl", "s1")
        a = log.emit("speech_start", turn_id=1, x=1)
        b = log.emit("speech_end", turn_id=1, generation_id=2)
        log.close()
        return a, b

    a, b = run(_async(body))
    rows = read_jsonl(tmp_path / "t.jsonl")
    assert (a, b) == (0, 1)
    assert set(rows[0]) == {"timestamp", "session_id", "turn_id", "generation_id", "event_type", "sequence_number", "payload"}
    assert rows[0]["payload"] == {"x": 1} and rows[1]["generation_id"] == 2


def test_listener_sees_each_event_synchronously(tmp_path):
    seen = []

    def body():
        log = EventLog(tmp_path / "t.jsonl", "s1")
        log.add_listener(seen.append)
        log.emit("speech_start", turn_id=1)
        assert len(seen) == 1
        log.emit("speech_end", turn_id=1)
        log.close()

    run(_async(body))
    assert [e["event_type"] for e in seen] == ["speech_start", "speech_end"]
    assert [e["sequence_number"] for e in seen] == [0, 1]


def test_required_event_types_are_declared():
    assert {"audio_frame", "speech_start", "speech_end", "endpoint_committed", "asr_partial", "asr_final", "llm_chunk",
            "tts_chunk", "audio_enqueued", "playback_started", "playback_progress", "playback_stopped",
            "generation_cancelled", "stale_event_dropped", "session_closed",
            "script_marker", "turn_score", "speculation_started", "speculation_adopted", "speculation_discarded",
            "playback_resumed", "playback_underrun", "bargein_decision", "context_committed", "queue_overflow",
            "provider_timeout", "task_failed", "generation_failed", "cancel_grace_exceeded",
            "close_timeout", "turn_started"} <= ALL_EVENT_TYPES


def test_config_defaults_match_design():
    c = RuntimeConfig()
    assert (c.max_silence_ms, c.noise_timeout_ms, c.playback_capacity_frames, c.cancel_grace_ms, c.close_grace_ms) == (1500, 400, 75, 1000, 2000)
    assert c.high_floor > c.low
