"""TruthLedger：每个 generation 的 Playback Truth 记账（设计 §8.4、§11.2）。"""
from dataclasses import dataclass

from voice_runtime.audio import FRAME_MS, FRAME_SAMPLES, AudioChunk, WordTiming


@dataclass
class _Chunk:
    text_range: tuple[int, int]
    samples: int
    word_timings: list[WordTiming]
    enqueued_frames: int = 0
    played_frames: int = 0
    dropped: tuple[str, str] | None = None  # (reason, gate)

    @property
    def state(self) -> str:
        if self.dropped:
            return "dropped"
        if self.played_frames:
            return "played"
        if self.enqueued_frames:
            return "enqueued"
        return "synthesized"


class TruthLedger:
    def __init__(self, generation_id: int):
        self.generation_id = generation_id
        self._full_text = ""
        self._chunks: dict[int, _Chunk] = {}

    def on_generated(self, text: str) -> None:
        self._full_text += text

    def on_synthesized(self, chunk_id: int, chunk: AudioChunk) -> None:
        self._chunks[chunk_id] = _Chunk(chunk.text_range, len(chunk.pcm) // 2, list(chunk.word_timings))

    def on_enqueued(self, chunk_id: int, frames: int) -> None:
        self._chunks[chunk_id].enqueued_frames += frames

    def on_played(self, chunk_id: int, frame_index: int) -> None:
        self._chunks[chunk_id].played_frames += 1

    def on_dropped(self, chunk_id: int, reason: str, gate: str) -> None:
        self._chunks[chunk_id].dropped = (reason, gate)

    def _heard_range(self) -> tuple[int, int] | None:
        start = end = None
        for _, c in sorted(self._chunks.items()):
            played_ms = c.played_frames * FRAME_MS
            pos, chunk_end = c.text_range
            for w in c.word_timings:
                if w.end_ms > played_ms:
                    return None if start is None else (start, end)
                at = self._full_text.find(w.word, pos, chunk_end)
                if at < 0:  # 词与文本对不上：保守地停在这里
                    return None if start is None else (start, end)
                if start is None:
                    start = at
                end = pos = at + len(w.word)
        return None if start is None else (start, end)

    def summary(self) -> dict:
        chunks = sorted(self._chunks.items())
        heard = self._heard_range()
        enq = [c for _, c in chunks if c.enqueued_frames > 0]
        return {
            "full_text": self._full_text,
            "enqueued_text": self._full_text[enq[0].text_range[0]:enq[-1].text_range[1]] if enq else "",
            "heard_text": self._full_text[heard[0]:heard[1]] if heard else "",
            "heard_range": list(heard) if heard else [0, 0],
            "played_ms": sum(c.played_frames for _, c in chunks) * FRAME_MS,
            "chunks": [{
                "chunk_id": cid,
                "text_range": list(c.text_range),
                "samples": c.samples,
                "enqueued": c.enqueued_frames > 0,
                "played_samples": min(c.played_frames * FRAME_SAMPLES, c.samples),
                "truncated": c.enqueued_frames > 0 and c.played_frames * FRAME_SAMPLES < c.samples,
                "state": c.state,
            } for cid, c in chunks],
        }
