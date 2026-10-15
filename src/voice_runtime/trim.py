"""TTS 开头底噪裁剪：能量阈值法（20ms 窗口，低于阈值的窗口视为底噪）。"""
from dataclasses import replace

from voice_runtime.audio import FRAME_SAMPLES, SAMPLE_RATE, AudioChunk, WordTiming, rms


def trim_leading_noise(chunk: AudioChunk, *, max_trim_ms: int, threshold_rms: float = 1000.0) -> AudioChunk:
    cap = min(len(chunk.pcm) // 2, SAMPLE_RATE * max_trim_ms // 1000)
    trimmed = 0
    while trimmed + FRAME_SAMPLES <= cap:
        window = chunk.pcm[trimmed * 2:(trimmed + FRAME_SAMPLES) * 2]
        if rms(window) >= threshold_rms:
            break
        trimmed += FRAME_SAMPLES
    if trimmed == 0:
        return chunk
    shift_ms = trimmed * 1000 // SAMPLE_RATE
    timings = [WordTiming(w.word, max(0, w.start_ms - shift_ms), max(0, w.end_ms - shift_ms))
               for w in chunk.word_timings]
    return replace(chunk, pcm=chunk.pcm[trimmed * 2:], word_timings=timings,
                   trimmed_samples=chunk.trimmed_samples + trimmed)
