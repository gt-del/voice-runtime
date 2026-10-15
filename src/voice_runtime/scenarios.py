"""场景定义（测试和 run_scenarios 共用）。场景构造函数在 Task 14 加入。"""
from dataclasses import dataclass, field

from voice_runtime.providers.fake import StreamFaults


@dataclass
class Scenario:
    name: str
    segments: list
    llm_replies: dict[str, str]
    default_reply: str
    llm_faults: StreamFaults
    tts_faults: StreamFaults
    close_after: tuple[str, int, int]  # (event_type, nth, delay_ms)：第 nth 个该事件之后 delay_ms 关闭
    asr_kwargs: dict = field(default_factory=dict)
    cfg_overrides: dict = field(default_factory=dict)
    tts_leading_noise_ms: int = 0
