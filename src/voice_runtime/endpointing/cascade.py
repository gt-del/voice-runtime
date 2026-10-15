"""分级判停：L1 打分 + 随静音时长下降的动态阈值 + 静音硬上限（设计 §6.2）。"""
import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal

from voice_runtime.config import RuntimeConfig
from voice_runtime.events import (PROVIDER_TIMEOUT, STALE_EVENT_DROPPED, TASK_FAILED, TURN_SCORE,
                                  EventLog)
from voice_runtime.providers.base import TurnScore, TurnScorer
from voice_runtime.speech import SpeechEdge
from voice_runtime.vloop import now_ms


def high_threshold(silence_ms: int, cfg: RuntimeConfig) -> float | None:
    if silence_ms < cfg.initial_silence_ms:
        return None
    if silence_ms >= cfg.threshold_floor_at_ms:
        return cfg.high_floor
    frac = (silence_ms - cfg.initial_silence_ms) / (cfg.threshold_floor_at_ms - cfg.initial_silence_ms)
    return cfg.high_start - (cfg.high_start - cfg.high_floor) * frac


@dataclass(frozen=True)
class EndpointDecision:
    reason: Literal["score_threshold", "max_silence"]
    level: int | None
    p: float | None
    threshold: float | None
    decided_at_ms: int


class EndpointCascade:
    def __init__(self, cfg: RuntimeConfig, log: EventLog, scorers: list[TurnScorer], *,
                 spawn: Callable[[Coroutine[Any, Any, None], str], asyncio.Task]):
        self._cfg = cfg
        self._log = log
        self._scorers = scorers  # scorers[0] 是 L1；L2/L3 的升级在 Task 15
        self._spawn = spawn
        self.turn_id: int | None = None
        self.on_l1_scored: Callable[[TurnScore, str], None] | None = None
        self.text_version = 0
        self.latest_l1: TurnScore | None = None  # 当前 (epoch, text_version) 下的 L1 结果
        self.have_final_for_last_segment = False
        self._finals: list[str] = []
        self._partial = ""
        self._epoch = 0
        self._silence_start_ms: int | None = None  # 非 None 表示正处于静音中
        self._decided = False

    @property
    def current_text(self) -> str:
        return " ".join(self._finals + ([self._partial] if self._partial else []))

    def on_speech_edge(self, edge: SpeechEdge) -> None:
        self._epoch = edge.epoch
        self.latest_l1 = None
        if edge.kind == "start":
            self._silence_start_ms = None
            self._decided = False
            self.have_final_for_last_segment = False  # 新语音段已开口，上一段的 final 不算数
        else:
            self._silence_start_ms = edge.ts_ms
            self._score_l1()

    def on_text(self, text: str, is_final: bool) -> None:
        before = self.current_text
        if is_final:
            if text:
                self._finals.append(text)
            self._partial = ""
        else:
            self._partial = text
        self.have_final_for_last_segment = is_final
        if self.current_text == before:
            return
        self.text_version += 1
        self.latest_l1 = None
        if self._silence_start_ms is not None:
            self._score_l1()

    def tick(self, now_ms: int) -> EndpointDecision | None:
        if self._decided or self._silence_start_ms is None or not self.current_text:
            return None
        silence = now_ms - self._silence_start_ms
        if silence >= self._cfg.max_silence_ms:
            return self._decide(EndpointDecision("max_silence", None, None, None, now_ms))
        th = high_threshold(silence, self._cfg)
        s = self.latest_l1
        if th is not None and s is not None and s.p_complete >= th and s.confidence >= self._cfg.min_conf:
            return self._decide(EndpointDecision("score_threshold", 1, s.p_complete, th, now_ms))
        return None

    def reset(self) -> None:
        self._finals = []
        self._partial = ""
        self.have_final_for_last_segment = False
        self.text_version += 1
        self.latest_l1 = None
        self._silence_start_ms = None  # 旧 speech_end 不再生效，需等新的 end 边沿
        self._decided = False

    def _decide(self, d: EndpointDecision) -> EndpointDecision:
        self._decided = True
        return d

    def _score_l1(self) -> None:
        text = self.current_text
        if text:
            self._spawn(self._run_l1(text, self._epoch, self.text_version, self.turn_id),
                        "session/score-l1")

    async def _run_l1(self, text: str, epoch: int, version: int, turn_id: int | None) -> None:
        # 事件的 turn_id 取发起请求时的值（设计 §8.1），迟到结果仍归属旧轮次。
        started = now_ms()
        score: TurnScore | None = None
        try:
            async with asyncio.timeout(self._cfg.l1_budget_ms / 1000):
                score = await self._scorers[0].score(text)
        except TimeoutError:
            self._log.emit(PROVIDER_TIMEOUT, turn_id=turn_id, level=1)
        except Exception as e:
            self._log.emit(TASK_FAILED, turn_id=turn_id, task="session/score-l1", level=1,
                           error=f"{type(e).__name__}: {e}")
        if (epoch, version) != (self._epoch, self.text_version):
            self._log.emit(STALE_EVENT_DROPPED, turn_id=turn_id, gate="endpoint", phase="late",
                           level=1, epoch=epoch, text_version=version)
            return
        self._log.emit(TURN_SCORE, turn_id=turn_id, level=1,
                       p=score.p_complete if score else None,
                       confidence=score.confidence if score else None,
                       rules=list(score.rules) if score else [],
                       latency_ms=now_ms() - started, epoch=epoch, text_version=version,
                       decision=self._classify(score))
        if score is None:
            return
        self.latest_l1 = score
        if self.on_l1_scored is not None:
            try:
                self.on_l1_scored(score, text)
            except Exception as e:
                self._log.emit(TASK_FAILED, turn_id=turn_id, task="session/score-l1",
                               error=f"{type(e).__name__}: {e}")

    def _classify(self, s: TurnScore | None) -> str:
        if s is None or s.confidence < self._cfg.min_conf:
            return "uncertain"
        if s.p_complete >= self._cfg.high_start:
            return "commit_candidate"
        if s.p_complete <= self._cfg.low:
            return "incomplete"
        return "uncertain"
