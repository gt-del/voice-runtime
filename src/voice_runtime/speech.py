"""语音段边界：VAD 帧概率 → speech_start / speech_end 边沿（end 回溯到第一帧静音）。"""
from dataclasses import dataclass
from typing import Literal

from voice_runtime.audio import Frame
from voice_runtime.config import RuntimeConfig

SPEECH_PROB = 0.5


@dataclass(frozen=True)
class SpeechEdge:
    kind: Literal["start", "end"]
    ts_ms: int
    epoch: int


class SpeechTracker:
    def __init__(self, cfg: RuntimeConfig):
        self._cfg = cfg
        self.in_speech = False
        self.epoch = 0
        self.speech_frames_run = 0
        self.last_speech_end_ms: int | None = None
        self.speech_start_ms: int | None = None  # 当前（或最近一段）语音的 start 边沿时间戳
        self._run_start_ms = 0
        self._silence_run = 0
        self._silence_start_ms = 0

    def feed(self, frame: Frame, prob: float) -> SpeechEdge | None:
        if prob >= SPEECH_PROB:
            if self.speech_frames_run == 0:
                self._run_start_ms = frame.ts_ms
            self.speech_frames_run += 1
            self._silence_run = 0
            if not self.in_speech and self.speech_frames_run >= self._cfg.vad_start_frames:
                self.in_speech = True
                self.epoch += 1
                self.speech_start_ms = self._run_start_ms
                return SpeechEdge("start", self._run_start_ms, self.epoch)
            return None
        self.speech_frames_run = 0
        if not self.in_speech:
            return None
        if self._silence_run == 0:
            self._silence_start_ms = frame.ts_ms
        self._silence_run += 1
        if self._silence_run >= self._cfg.vad_end_frames:
            self.in_speech = False
            self._silence_run = 0
            self.last_speech_end_ms = self._silence_start_ms
            return SpeechEdge("end", self._silence_start_ms, self.epoch)
        return None
