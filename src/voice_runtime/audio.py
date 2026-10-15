"""音频帧模型：单声道 / PCM16 小端 / 16kHz / 20ms 一帧。"""
import math
import string
import struct
from dataclasses import dataclass, field

SAMPLE_RATE = 16000
FRAME_MS = 20
FRAME_SAMPLES = 320


@dataclass(frozen=True)
class Frame:
    seq: int
    ts_ms: int
    pcm: bytes


@dataclass(frozen=True)
class WordTiming:
    word: str
    start_ms: int
    end_ms: int


@dataclass
class AudioChunk:
    pcm: bytes
    text_range: tuple[int, int]
    word_timings: list[WordTiming] = field(default_factory=list)
    trimmed_samples: int = 0


def tone(ms: int, amplitude: int) -> bytes:
    """确定性方波（每 8 个采样翻转一次符号），amplitude=0 即静音。"""
    n = SAMPLE_RATE * ms // 1000
    return struct.pack(f"<{n}h", *[amplitude if (i // 8) % 2 == 0 else -amplitude for i in range(n)])


def rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", pcm)
    return math.sqrt(sum(s * s for s in samples) / n)


def word_duration_ms(word: str) -> int:
    """50 + 10 * 去标点后的字数，向上取整到 FRAME_MS 的倍数。"""
    raw = 50 + 10 * len(word.strip(string.punctuation))
    return -(-raw // FRAME_MS) * FRAME_MS
