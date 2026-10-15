"""事件类型常量与同步 JSONL 事件日志（设计 §11.3）。"""
import json
from collections.abc import Callable
from pathlib import Path

from voice_runtime.vloop import now_ms

AUDIO_FRAME = "audio_frame"
SPEECH_START = "speech_start"
SPEECH_END = "speech_end"
ENDPOINT_COMMITTED = "endpoint_committed"
ASR_PARTIAL = "asr_partial"
ASR_FINAL = "asr_final"
LLM_CHUNK = "llm_chunk"
TTS_CHUNK = "tts_chunk"
AUDIO_ENQUEUED = "audio_enqueued"
PLAYBACK_STARTED = "playback_started"
PLAYBACK_PROGRESS = "playback_progress"
PLAYBACK_STOPPED = "playback_stopped"
GENERATION_CANCELLED = "generation_cancelled"
STALE_EVENT_DROPPED = "stale_event_dropped"
SESSION_CLOSED = "session_closed"
SCRIPT_MARKER = "script_marker"
TURN_SCORE = "turn_score"
SPECULATION_STARTED = "speculation_started"
SPECULATION_ADOPTED = "speculation_adopted"
SPECULATION_DISCARDED = "speculation_discarded"
PLAYBACK_RESUMED = "playback_resumed"
PLAYBACK_UNDERRUN = "playback_underrun"
BARGEIN_DECISION = "bargein_decision"
CONTEXT_COMMITTED = "context_committed"
QUEUE_OVERFLOW = "queue_overflow"
PROVIDER_TIMEOUT = "provider_timeout"
TASK_FAILED = "task_failed"
GENERATION_FAILED = "generation_failed"
CANCEL_GRACE_EXCEEDED = "cancel_grace_exceeded"
CLOSE_TIMEOUT = "close_timeout"
TURN_STARTED = "turn_started"

ALL_EVENT_TYPES: frozenset[str] = frozenset({
    AUDIO_FRAME,
    SPEECH_START,
    SPEECH_END,
    ENDPOINT_COMMITTED,
    ASR_PARTIAL,
    ASR_FINAL,
    LLM_CHUNK,
    TTS_CHUNK,
    AUDIO_ENQUEUED,
    PLAYBACK_STARTED,
    PLAYBACK_PROGRESS,
    PLAYBACK_STOPPED,
    GENERATION_CANCELLED,
    STALE_EVENT_DROPPED,
    SESSION_CLOSED,
    SCRIPT_MARKER,
    TURN_SCORE,
    SPECULATION_STARTED,
    SPECULATION_ADOPTED,
    SPECULATION_DISCARDED,
    PLAYBACK_RESUMED,
    PLAYBACK_UNDERRUN,
    BARGEIN_DECISION,
    CONTEXT_COMMITTED,
    QUEUE_OVERFLOW,
    PROVIDER_TIMEOUT,
    TASK_FAILED,
    GENERATION_FAILED,
    CANCEL_GRACE_EXCEEDED,
    CLOSE_TIMEOUT,
    TURN_STARTED,
})


def _dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class EventLog:
    def __init__(self, path: Path, session_id: str):
        self._file = open(path, "a", buffering=1, encoding="utf-8")
        self._session_id = session_id
        self._seq = 0
        self._listeners: list[Callable[[dict], None]] = []

    def emit(self, event_type: str, *, turn_id: int | None = None,
             generation_id: int | None = None, **payload) -> int:
        seq = self._seq
        self._seq += 1
        event = {
            "timestamp": now_ms(),
            "session_id": self._session_id,
            "turn_id": turn_id,
            "generation_id": generation_id,
            "event_type": event_type,
            "sequence_number": seq,
            "payload": payload,
        }
        self._file.write(_dumps(event) + "\n")
        for fn in self._listeners:
            fn(event)
        return seq

    def add_listener(self, fn: Callable[[dict], None]) -> None:
        self._listeners.append(fn)

    def close(self) -> None:
        self._file.close()


def read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
