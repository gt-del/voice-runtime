"""Provider 协议与跨 Provider 共用的数据类型。"""
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar, runtime_checkable

from voice_runtime.audio import AudioChunk, Frame

T = TypeVar("T")


@dataclass(frozen=True)
class AsrWord:
    w: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class AsrEvent:
    kind: Literal["partial", "final"]
    text: str
    words: tuple[AsrWord, ...]
    segment_id: int
    segment_start_ms: int
    segment_end_ms: int


@dataclass(frozen=True)
class TurnScore:
    p_complete: float
    confidence: float
    rules: tuple[str, ...] = ()


class ProviderStream(Protocol[T]):
    def __aiter__(self) -> "ProviderStream[T]": ...
    async def __anext__(self) -> T: ...
    def cancel(self) -> None: ...


@runtime_checkable
class VAD(Protocol):
    def process(self, frame: Frame) -> float: ...


class ASR(Protocol):
    def run(self, frames: AsyncIterator[Frame]) -> AsyncIterator[AsrEvent]: ...


class LLM(Protocol):
    def start(self, messages: list[dict]) -> ProviderStream[str]: ...


class TTS(Protocol):
    def start(self, text_stream: AsyncIterator[str]) -> ProviderStream[AudioChunk]: ...


@runtime_checkable
class TurnScorer(Protocol):
    def score(self, text: str, audio: bytes | None = None) -> Awaitable[TurnScore]: ...


class BackchannelScorer(Protocol):
    def score(self, slices: list[bytes]) -> Awaitable[list[float]]: ...
