import asyncio

import pytest

from voice_runtime.audio import Frame, tone, word_duration_ms
from voice_runtime.events import EventLog
from voice_runtime.providers.base import TurnScore
from voice_runtime.providers.fake import (FakeASR, FakeBackchannelScorer, FakeLLM, FakeTTS, FakeVAD,
                                          ScriptedTurnScorer, StreamFaults, normalize_text)
from voice_runtime.script import Noise, ScriptedAudioSource, Silence, Speech
from voice_runtime.vloop import now_ms, run

LONG = "one two three four five six seven eight"


def _llm(faults):
    return FakeLLM({"hi": LONG}, "default reply", faults)


async def _timed(stream):
    out = []
    async for item in stream:
        out.append((now_ms(), item))
    return out


def _llm_run(faults):
    async def body():
        t0 = now_ms()
        return t0, await _timed(_llm(faults).start([{"role": "user", "content": "Hi!"}]))
    return run(body())


def test_fake_llm_first_delay_interval_and_jitter_are_seeded():
    f = StreamFaults(first_delay_ms=300, interval_ms=100, jitter_ms=20, seed=7)
    t0, a = _llm_run(f)
    _, b = _llm_run(f)
    assert [t - t0 for t, _ in a] == [t - t0 for t, _ in b]
    assert "".join(x for _, x in a) == LONG + " "
    times = [t - t0 for t, _ in a]
    assert 300 <= times[0] <= 320
    assert all(100 <= y - x <= 120 for x, y in zip(times, times[1:]))


def test_cancel_then_late_chunks_arrive_at_configured_offsets():
    f = StreamFaults(100, 100, jitter_ms=0, late_after_cancel_ms=(50, 900))

    async def body():
        s = _llm(f).start([{"role": "user", "content": "hi"}])
        await anext(s)
        t = now_ms()
        s.cancel()
        return t, await _timed(s)

    t, rest = run(body())
    assert [x - t for x, _ in rest] == [50, 900]


def test_hang_after_is_cancellable():
    async def body():
        s = _llm(StreamFaults(100, 100, hang_after=1)).start([{"role": "user", "content": "hi"}])
        await anext(s)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anext(s), 5)

    run(body())


def test_raise_at_raises_runtime_error():
    async def body():
        s = _llm(StreamFaults(100, 100, raise_at=1)).start([{"role": "user", "content": "hi"}])
        await anext(s)
        with pytest.raises(RuntimeError, match="fake failure"):
            await anext(s)

    run(body())


def test_swallow_first_cancel_needs_two_cancels():
    async def body():
        s = _llm(StreamFaults(100, 100, hang_after=0, swallow_first_cancel=True)).start([])
        task = asyncio.create_task(anext(s), name="test/consumer")
        await asyncio.sleep(1)
        task.cancel()
        await asyncio.sleep(1)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(body())


async def _feed(parts):
    for delay, text in parts:
        if delay:
            await asyncio.sleep(delay / 1000)
        yield text


def test_fake_tts_first_chunk_delay_counts_from_first_text():
    async def body():
        tts = FakeTTS(StreamFaults(200, 100, jitter_ms=0))
        return await _timed(tts.start(_feed([(500, "hello world "), (0, "again ")])))

    out = run(body())
    assert out[0][0] >= 700
    assert out[1][0] - out[0][0] >= 100


def test_fake_tts_chunk_word_timings_cover_text_range():
    text = "Please book it for Wednesday afternoon"
    words = text.split()

    async def body():
        tts = FakeTTS(StreamFaults(100, 20, jitter_ms=0), leading_noise_ms=60)
        parts = [(10, w + " ") for w in words[:-1]] + [(10, words[-1])]
        return [c for _, c in await _timed(tts.start(_feed(parts)))]

    chunks = run(body())
    assert "".join(text[a:b] for a, b in (c.text_range for c in chunks)) == text
    assert sum(len(c.pcm) // 2 for c in chunks) == 16 * (sum(map(word_duration_ms, words)) + 60)
    first = chunks[0]
    assert first.word_timings[0].start_ms == 60
    assert all(c.word_timings[0].start_ms == 0 for c in chunks[1:])


def test_fake_asr_partials_finals_and_empty_final_for_noise(tmp_path):
    async def body():
        log = EventLog(tmp_path / "e.jsonl", "s")
        segs = [Speech("Stop. Make it"), Silence(1000), Noise(80), Silence(1000)]
        src = ScriptedAudioSource(segs, log)
        out = []
        async for ev in FakeASR(segs).run(src.frames()):
            out.append(ev)
            if sum(e.kind == "final" for e in out) == 2:
                break
        return out

    out = run(body())
    assert [e.text for e in out] == ["Stop", "Stop Make", "Stop Make it", "Stop. Make it", ""]
    assert [e.kind for e in out] == ["partial"] * 3 + ["final"] * 2
    assert [e.segment_id for e in out] == [0, 0, 0, 0, 1]
    fin = out[3]
    assert (fin.segment_start_ms, fin.segment_end_ms) == (0, 280)
    assert [(w.w, w.start_ms, w.end_ms) for w in fin.words] == [
        ("Stop.", 0, 100), ("Make", 100, 200), ("it", 200, 280)]


def test_fake_asr_hang_at_stops_consuming(tmp_path):
    async def body():
        segs = [Silence(5000)]
        log = EventLog(tmp_path / "e.jsonl", "s")
        src = ScriptedAudioSource(segs, log)
        it = FakeASR(segs, hang_at_ms=100).run(src.frames())
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(anext(it), 10)

    run(body())


def test_fake_vad_uses_energy_not_labels():
    vad = FakeVAD()
    assert vad.process(Frame(0, 0, tone(20, 3000))) == 0.9
    assert vad.process(Frame(1, 20, tone(20, 200))) == 0.05


def test_scorers():
    async def body():
        ts = ScriptedTurnScorer({"stop": TurnScore(0.9, 0.8)}, TurnScore(0.1, 0.2), delay_ms=50)
        assert (await ts.score("Stop!")).p_complete == 0.9
        assert (await ts.score("other")).p_complete == 0.1
        with pytest.raises(RuntimeError):
            await ScriptedTurnScorer({}, TurnScore(0, 0), raise_exc=True).score("x")
        assert await FakeBackchannelScorer().score([b"", b""]) == [0.8, 0.8]
        assert await FakeBackchannelScorer(lambda n: [0.1] * n).score([b""]) == [0.1]
        with pytest.raises(RuntimeError):
            await FakeBackchannelScorer(fail=True).score([b""])

    run(body())


def test_normalize_text():
    assert normalize_text("  Please, book IT for Wednesday!") == "please book it for wednesday"


def _tts_text():
    return _feed([(0, "alpha beta "), (5000, "gamma delta ")])


def _cancel_midflight(make, late, hang_after=None):
    async def body():
        s = make(StreamFaults(100, 100, jitter_ms=0, late_after_cancel_ms=late, hang_after=hang_after))
        task = asyncio.create_task(_timed(s), name="test/consumer")
        await asyncio.sleep(0.5 if hang_after is None else 2)
        t = now_ms()
        s.cancel()
        return t, await asyncio.wait_for(task, 10)

    return run(body())


def _llm_stream(f):
    return FakeLLM({}, LONG, f).start([])


def _tts_stream(f):
    return FakeTTS(f).start(_tts_text())


@pytest.mark.parametrize("make", [_llm_stream, _tts_stream])
def test_cancel_from_other_task_without_late_ends_stream(make):
    t, out = _cancel_midflight(make, ())
    assert all(x <= t for x, _ in out)  # cancel 之后没有多出来的项


@pytest.mark.parametrize("make", [_llm_stream, _tts_stream])
def test_cancel_from_other_task_with_one_late_item(make):
    t, out = _cancel_midflight(make, (50,))
    assert [x - t for x, _ in out if x > t] == [50]


def test_cancel_during_hang_releases_it():
    t, out = _cancel_midflight(_llm_stream, (50,), hang_after=1)
    assert [x - t for x, _ in out if x > t] == [50]


def test_swallow_first_cancel_keeps_cancelling_count_consistent():
    async def body():
        s = _llm_stream(StreamFaults(100, 100, hang_after=0, swallow_first_cancel=True))
        task = asyncio.create_task(anext(s), name="test/consumer")
        await asyncio.sleep(1)
        task.cancel()
        await asyncio.sleep(1)
        assert task.cancelling() == 0
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(body())
