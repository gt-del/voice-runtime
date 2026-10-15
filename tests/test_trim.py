from dataclasses import replace

from helpers import run_scenario

from voice_runtime.audio import AudioChunk, WordTiming, tone
from voice_runtime.scenarios import scenario_a_prime
from voice_runtime.trim import trim_leading_noise


def _chunk(noise_ms: int, speech_ms: int = 200) -> AudioChunk:
    pcm = tone(noise_ms, 200) + tone(speech_ms, 3000)
    return AudioChunk(pcm, (0, 5), [WordTiming("hello", noise_ms, noise_ms + speech_ms)])


def test_trims_leading_noise_and_shifts_word_timings():
    out = trim_leading_noise(_chunk(120), max_trim_ms=300)
    assert out.trimmed_samples == 1920
    assert len(out.pcm) == 2 * 16 * 200
    assert out.word_timings[0].start_ms == 0 and out.word_timings[0].end_ms == 200


def test_never_trims_speech():
    c = _chunk(0)
    out = trim_leading_noise(c, max_trim_ms=300)
    assert out.pcm == c.pcm and out.trimmed_samples == 0 and out.word_timings == c.word_timings


def test_trim_capped_at_max():
    out = trim_leading_noise(_chunk(500), max_trim_ms=300)
    assert out.trimmed_samples == 16 * 300


def test_first_audio_excludes_leading_noise_in_session(tmp_path):
    """playback_started 在首帧写出时触发，与帧内容无关；裁剪省掉的是首帧里底噪的播放时间，
    所以“可听到语音”的时刻 = playback_started 的时间 + 首帧里剩余的底噪时长。"""
    noise_ms = 120
    sc = replace(scenario_a_prime(), tts_leading_noise_ms=noise_ms)
    for sub in ("on", "off"):
        (tmp_path / sub).mkdir()
    on = run_scenario(sc, tmp_path / "on")
    off = run_scenario(sc, tmp_path / "off", trim_enabled=False)

    def audible_ms(r):
        first = [e for e in r.events if e["event_type"] == "tts_chunk"][0]["payload"]
        left_ms = noise_ms - first["trimmed_samples"] * 1000 // 16000
        return r.metrics["turn_end_to_first_audio_ms"][0] + left_ms, first

    a_on, first_on = audible_ms(on)
    a_off, first_off = audible_ms(off)
    assert abs((a_off - a_on) - 120) <= 20
    assert first_on["trimmed_samples"] == 1920 and first_off["trimmed_samples"] == 0
    assert first_off["samples"] - first_on["samples"] == 1920
