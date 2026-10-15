"""脚本化音频源：按确定的时间线产出 20ms 帧，并写入只供指标读取的 script_marker 真值。"""
import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from voice_runtime.audio import FRAME_MS, Frame, WordTiming, tone, word_duration_ms
from voice_runtime.events import SCRIPT_MARKER, EventLog
from voice_runtime.vloop import now_ms


@dataclass
class Speech:
    text: str
    amplitude: int = 3000


@dataclass
class Silence:
    ms: int


@dataclass
class Noise:
    ms: int
    amplitude: int = 12000


@dataclass
class AfterEvent:
    event_type: str
    delay_ms: int
    nth: int = 1


@dataclass
class Disconnect:
    pass


def speech_word_times(text: str, start_ms: int) -> list[WordTiming]:
    out, t = [], start_ms
    for w in text.split():
        d = word_duration_ms(w)
        out.append(WordTiming(w, t, t + d))
        t += d
    return out


class ScriptedAudioSource:
    def __init__(self, segments: list, log: EventLog):
        self._segments = segments
        self._log = log
        self._seq = 0
        self._watch = {(g.event_type, g.nth) for g in segments if isinstance(g, AfterEvent)}
        self._counts: dict[str, int] = {et: 0 for et, _ in self._watch}
        self._times: dict[tuple[str, int], int] = {}
        self.disconnected = False
        log.add_listener(self._on_event)

    def _on_event(self, event: dict) -> None:
        et = event["event_type"]
        if et not in self._counts:
            return
        n = self._counts[et] = self._counts[et] + 1
        if (et, n) in self._watch:
            self._times[(et, n)] = event["timestamp"]

    def _marker(self, segment: str, phase: str, index: int, text: str = "") -> None:
        self._log.emit(SCRIPT_MARKER, eval_only=True, segment=segment, phase=phase,
                       index=index, text=text)

    async def _emit(self, pcm: bytes) -> AsyncIterator[Frame]:
        frame = Frame(self._seq, self._seq * FRAME_MS, pcm)
        self._seq += 1
        yield frame
        await asyncio.sleep(FRAME_MS / 1000)

    async def _segment(self, index: int, kind: str, text: str, pcm: bytes,
                       n_frames: int) -> AsyncIterator[Frame]:
        self._marker(kind, "start", index, text)
        for _ in range(n_frames):
            async for f in self._emit(pcm):
                yield f
        self._marker(kind, "end", index, text)

    async def frames(self) -> AsyncIterator[Frame]:
        silence = tone(FRAME_MS, 0)
        for index, seg in enumerate(self._segments):
            if isinstance(seg, Speech):
                n = sum(word_duration_ms(w) for w in seg.text.split()) // FRAME_MS
                gen = self._segment(index, "speech", seg.text, tone(FRAME_MS, seg.amplitude), n)
            elif isinstance(seg, Noise):
                gen = self._segment(index, "noise", "", tone(FRAME_MS, seg.amplitude),
                                    -(-seg.ms // FRAME_MS))
            elif isinstance(seg, Silence):
                gen = self._segment(index, "silence", "", silence, -(-seg.ms // FRAME_MS))
            elif isinstance(seg, AfterEvent):
                while True:
                    t = self._times.get((seg.event_type, seg.nth))
                    if t is not None and self._seq * FRAME_MS >= t + seg.delay_ms:
                        break
                    async for f in self._emit(silence):
                        yield f
                continue
            elif isinstance(seg, Disconnect):
                self._marker("disconnect", "start", index)
                self._marker("disconnect", "end", index)
                self.disconnected = True
                return
            else:
                raise TypeError(f"unknown segment type: {type(seg).__name__}")
            async for f in gen:
                yield f
        while True:
            async for f in self._emit(silence):
                yield f
