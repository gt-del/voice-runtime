import asyncio

from voice_runtime.audio import rms, tone, word_duration_ms
from voice_runtime.events import EventLog, SCRIPT_MARKER, read_jsonl
from voice_runtime.script import (AfterEvent, Disconnect, Noise, ScriptedAudioSource,
                                  Silence, Speech, speech_word_times)
from voice_runtime.vloop import run


def _markers(path):
    return [(r["timestamp"], r["payload"]) for r in read_jsonl(path) if r["event_type"] == SCRIPT_MARKER]


def test_frames_are_320_samples_with_monotonic_seq_and_ts(tmp_path):
    async def body():
        log = EventLog(tmp_path / "e.jsonl", "s")
        src = ScriptedAudioSource([Silence(1000)], log)
        out = []
        async for f in src.frames():
            out.append(f)
            if len(out) == 10:
                break
        return out

    frames = run(body())
    assert [f.seq for f in frames] == list(range(10))
    assert [f.ts_ms for f in frames] == [i * 20 for i in range(10)]
    assert all(len(f.pcm) == 640 for f in frames)


def test_word_duration_rounds_up_to_frame():
    assert [word_duration_ms(w) for w in ("Please", "book", "it.")] == [120, 100, 80]


def test_speech_word_times_back_to_back():
    wt = speech_word_times("Please book it.", 100)
    assert [(w.word, w.start_ms, w.end_ms) for w in wt] == [
        ("Please", 100, 220), ("book", 220, 320), ("it.", 320, 400)]


def test_markers_record_true_speech_boundaries(tmp_path):
    async def body():
        log = EventLog(tmp_path / "e.jsonl", "s")
        src = ScriptedAudioSource([Silence(100), Speech("Please book it.")], log)
        n = 0
        async for _ in src.frames():
            n += 1
            if n == 30:
                break

    run(body())
    ms = [(t, p) for t, p in _markers(tmp_path / "e.jsonl") if p["segment"] == "speech"]
    assert [(t, p["phase"]) for t, p in ms] == [(100, "start"), (400, "end")]
    assert ms[0][1]["eval_only"] is True and ms[0][1]["index"] == 1
    assert ms[0][1]["text"] == "Please book it."


def test_after_event_waits_for_runtime_event(tmp_path):
    async def body():
        log = EventLog(tmp_path / "e.jsonl", "s")
        src = ScriptedAudioSource([AfterEvent("playback_started", 1200), Noise(80)], log)

        async def emitter():
            await asyncio.sleep(0.5)
            log.emit("playback_started")

        t = asyncio.create_task(emitter(), name="test/emitter")
        n = 0
        async for f in src.frames():
            n += 1
            if n == 100:
                break
        await t

    run(body())
    ms = [(t, p) for t, p in _markers(tmp_path / "e.jsonl") if p["segment"] == "noise"]
    assert [(t, p["phase"]) for t, p in ms] == [(1700, "start"), (1780, "end")]


def test_disconnect_ends_iteration_and_silence_continues_otherwise(tmp_path):
    async def body():
        log = EventLog(tmp_path / "e.jsonl", "s")
        src = ScriptedAudioSource([Silence(40), Disconnect()], log)
        frames = [f async for f in src.frames()]
        src2 = ScriptedAudioSource([Silence(40)], log)
        tail = []
        async for f in src2.frames():
            tail.append(f)
            if len(tail) == 50:
                break
        return src, frames, src2, tail

    src, frames, src2, tail = run(body())
    assert len(frames) == 2 and src.disconnected and not src2.disconnected
    assert len(tail) == 50 and rms(tail[-1].pcm) == 0
    assert any(p["segment"] == "disconnect" for _, p in _markers(tmp_path / "e.jsonl"))


def test_speech_louder_than_threshold_noise_louder_still():
    assert rms(tone(20, 12000)) > rms(tone(20, 3000)) > 1000 > rms(tone(20, 0))


def test_unrelated_events_do_not_grow_source_state(tmp_path):
    async def body():
        log = EventLog(tmp_path / "e.jsonl", "s")
        src = ScriptedAudioSource([AfterEvent("playback_started", 0), Silence(20)], log)
        for _ in range(1000):
            log.emit("audio_frame")
        log.emit("playback_started")
        return src

    src = run(body())
    assert len(src._counts) == 1 and len(src._times) == 1


def test_unknown_segment_type_raises_type_error(tmp_path):
    async def body():
        log = EventLog(tmp_path / "e.jsonl", "s")
        src = ScriptedAudioSource(["bogus"], log)
        async for _ in src.frames():
            pass

    import pytest
    with pytest.raises(TypeError):
        run(body())
