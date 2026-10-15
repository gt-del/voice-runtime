"""测试工具：按 Scenario 构造 Session 并在虚拟时钟上跑完。"""
import asyncio
from dataclasses import dataclass
from pathlib import Path

from voice_runtime.config import RuntimeConfig
from voice_runtime.endpointing.rules import RuleTurnScorer
from voice_runtime.events import EventLog
from voice_runtime.metrics import compute_metrics
from voice_runtime.playback import FakeSink
from voice_runtime.providers.fake import (FakeASR, FakeBackchannelScorer, FakeLLM, FakeTTS,
                                          FakeVAD)
from voice_runtime.scenarios import Scenario
from voice_runtime.script import ScriptedAudioSource
from voice_runtime.session import CloseReason, Providers, Session, SessionState
from voice_runtime.vloop import run


@dataclass
class ScenarioResult:
    events: list[dict]
    metrics: dict
    sink: FakeSink
    reason: CloseReason
    session: Session  # 供测试读取 context / queues
    state_at_close: SessionState  # 触发 close 那一刻的状态（run 结束后 state 必为 CLOSED）


def build_session(sc: Scenario, cfg: RuntimeConfig, log: EventLog) -> tuple[Session, FakeSink]:
    source = ScriptedAudioSource(sc.segments, log)
    providers = Providers(
        vad=FakeVAD(),
        asr=FakeASR(sc.segments, **sc.asr_kwargs),
        llm=FakeLLM(sc.llm_replies, sc.default_reply, sc.llm_faults),
        tts=FakeTTS(sc.tts_faults, leading_noise_ms=sc.tts_leading_noise_ms),
        l1=RuleTurnScorer(),
        backchannel=FakeBackchannelScorer(),
    )
    sink = FakeSink()
    return Session(cfg, providers, source, sink, log), sink


def run_scenario(sc: Scenario, tmp_path: Path, *, max_virtual_ms: int = 60000,
                 **cfg_overrides) -> ScenarioResult:
    cfg = RuntimeConfig(**{**sc.cfg_overrides, **cfg_overrides})
    log = EventLog(tmp_path / f"{sc.name}.jsonl", "s1")
    events: list[dict] = []
    log.add_listener(events.append)
    try:
        session, sink = build_session(sc, cfg, log)
        forced = False
        state_at_close: SessionState | None = None

        def close() -> None:
            nonlocal state_at_close
            if state_at_close is None:
                state_at_close = session.state
            session.close()

        async def main() -> CloseReason:
            nonlocal forced
            loop = asyncio.get_running_loop()
            event_type, nth, delay_ms = sc.close_after
            seen = 0

            def on_event(e: dict) -> None:
                nonlocal seen
                if e["event_type"] == event_type:
                    seen += 1
                    if seen == nth:
                        loop.call_later(delay_ms / 1000, close)

            def force_close() -> None:
                nonlocal forced
                forced = True
                close()

            log.add_listener(on_event)
            handle = loop.call_later(max_virtual_ms / 1000, force_close)
            try:
                return await session.run()
            finally:
                handle.cancel()

        reason = run(main())
        if forced:
            raise AssertionError(
                f"{sc.name}: close_after {sc.close_after} 未在 {max_virtual_ms}ms 内触发")
        return ScenarioResult(events, compute_metrics(events), sink, reason, session, state_at_close)
    finally:
        log.close()
