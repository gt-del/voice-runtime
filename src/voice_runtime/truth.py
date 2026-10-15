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
        if self.played_frames:  # 播过一部分也算 played（truncated 标出没播完），不会再变成 dropped
            return "played"
        if self.dropped:
            return "dropped"
        if self.enqueued_frames:
            return "enqueued"
        return "synthesized"


class TruthLedger:
    def __init__(self, generation_id: int):
        self.generation_id = generation_id
        self._full_text = ""
        self._chunks: dict[int, _Chunk] = {}
        self._audible: list[list] = []  # 按播放顺序：[chunk_id, 词序号, 词, 最后一帧写入时刻]

    def on_generated(self, text: str) -> None:
        self._full_text += text

    def on_synthesized(self, chunk_id: int, chunk: AudioChunk) -> None:
        self._chunks[chunk_id] = _Chunk(chunk.text_range, len(chunk.pcm) // 2, list(chunk.word_timings))

    def on_enqueued(self, chunk_id: int, frames: int) -> None:
        self._chunks[chunk_id].enqueued_frames += frames

    def on_played(self, chunk_id: int, frame_index: int, at_ms: int) -> None:
        c = self._chunks[chunk_id]
        pos = c.played_frames * FRAME_MS  # 这一帧在 chunk 内的起点
        c.played_frames += 1
        i = next((i for i, w in enumerate(c.word_timings) if w.start_ms <= pos < w.end_ms), None)
        if i is None:  # 开头底噪
            return
        if self._audible and self._audible[-1][:2] == [chunk_id, i]:
            self._audible[-1][3] = at_ms
        else:
            self._audible.append([chunk_id, i, c.word_timings[i].word, at_ms])

    def played_words_since(self, since_ms: int) -> list[str]:
        """播放时间与 [since_ms, 现在] 有重叠的词，按播放顺序（门禁据此识别回声，§9.4）。"""
        return [w for _, _, w, last in self._audible if last + FRAME_MS > since_ms]

    def on_dropped(self, chunk_id: int, reason: str, gate: str) -> None:
        self._chunks[chunk_id].dropped = (reason, gate)

    def drop_unplayed(self, reason: str, gate: str) -> None:
        """已入队但一帧都没播的 chunk 记为 dropped(reason, gate)：invalidate 清除 playback 队列时清掉的正是它们的帧。
        播过一部分的 chunk 剩余帧同样被清掉，但它仍是 played（truncated=true），不记 dropped。"""
        for c in self._chunks.values():
            if c.dropped is None and c.played_frames == 0 and c.enqueued_frames > 0:
                c.dropped = (reason, gate)

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
                **({"reason": c.dropped[0], "gate": c.dropped[1]} if c.state == "dropped" else {}),
            } for cid, c in chunks],
        }
