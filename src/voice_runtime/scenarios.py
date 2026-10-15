"""场景定义（测试和 run_scenarios 共用）：A / A' / B / C / D / E（设计 §12），以及 F（回声，§9.4）。"""
from dataclasses import dataclass, field

from voice_runtime.providers.fake import StreamFaults
from voice_runtime.script import AfterEvent, Echo, Noise, Silence, Speech


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


LLM = StreamFaults(300, 50)  # 假 LLM 首包 / 间隔（§16）
TTS = StreamFaults(200, 40)  # 假 TTS 首包 / 间隔（§16）
REQUEST = "Book a table for two please."
SHORT_REPLY = "Sure, a table for two is booked."
# 按 word_duration_ms 合计约 4s（测试断言 3600～4400ms），保证打断时还在播
LONG_REPLY = ("Sure, I can help with that. Your table for two is booked for Thursday at seven in "
              "the evening, and I have added a note about the quiet window seat you asked for "
              "earlier today, so everything is ready.")
STOP = "Stop. Make it Friday instead."
FRIDAY_REPLY = "OK, Friday it is. I have moved your booking to Friday evening at seven."
# 第 n 条 context_committed 之后再等 300ms 关闭：此时该轮回复已播完（播完才提交上下文）
SETTLE_MS = 300


def _a(name: str, cfg_overrides: dict) -> Scenario:
    return Scenario(name=name, segments=[Speech(REQUEST)], llm_replies={},
                    default_reply=SHORT_REPLY, llm_faults=LLM, tts_faults=TTS,
                    close_after=("context_committed", 2, SETTLE_MS), cfg_overrides=cfg_overrides)


def scenario_a() -> Scenario:
    """A：开启预生成（P1，Task 16 使用）。"""
    return _a("A", {})


def scenario_a_prime() -> Scenario:
    """A'：关闭预生成，先判停、再 ASR / LLM / TTS。"""
    return _a("A_prime", {"speculation_enabled": False})


def scenario_b() -> Scenario:
    """B：说到一半停顿 700ms 再说完。"""
    segs = [Speech("Please book it for"), Silence(700), Speech("Wednesday afternoon.")]
    return Scenario(name="B", segments=segs, llm_replies={}, default_reply=SHORT_REPLY,
                    llm_faults=LLM, tts_faults=TTS, close_after=("context_committed", 2, SETTLE_MS))


def _barge_in(name: str, tts: StreamFaults, cfg_overrides: dict) -> Scenario:
    segs = [Speech(REQUEST), AfterEvent("playback_started", 1200), Speech(STOP)]
    # 第 4 条 context_committed 是新一轮助手回复播完后的提交
    return Scenario(name=name, segments=segs, llm_replies={STOP: FRIDAY_REPLY},
                    default_reply=LONG_REPLY, llm_faults=LLM, tts_faults=tts,
                    close_after=("context_committed", 4, SETTLE_MS), cfg_overrides=cfg_overrides)


def scenario_c() -> Scenario:
    """C：机器人说话 1.2s 后用户说 "Stop. Make it Friday instead."。"""
    return _barge_in("C", TTS, {})


def scenario_d() -> Scenario:
    """D：机器人说话 1s 后出现 80ms 噪声。"""
    segs = [Speech(REQUEST), AfterEvent("playback_started", 1000), Noise(80)]
    return Scenario(name="D", segments=segs, llm_replies={}, default_reply=LONG_REPLY,
                    llm_faults=LLM, tts_faults=TTS, close_after=("context_committed", 2, SETTLE_MS))


def scenario_e() -> Scenario:
    """E：同 C，但旧 generation 的 TTS 在 cancel 后再吐 2 个 chunk（+50ms、+1500ms）。
    新首帧距 cancel 约 1.3s（用户说完 + 判停 + LLM/TTS 首包），新回复要播 1s 多，所以第 2 个迟到 chunk
    恰好在新 generation 播放期间到达、在闸②被拦下；cancel_grace_ms 调到 2000 > 1500，保证它到达前
    看门狗不会取消；close_grace_ms 须 > cancel_grace_ms。
    关闭预生成：假 TTS 对每个被取消的流都会吐迟到 chunk，被替换的预生成会混进来，E 只看被打断的那个。"""
    tts = StreamFaults(200, 40, late_after_cancel_ms=(50, 1500))
    return _barge_in("E", tts, {"cancel_grace_ms": 2000, "close_grace_ms": 3000,
                                "speculation_enabled": False})


def scenario_f() -> Scenario:
    """F：机器人说话 1s 后，它自己的声音漏进麦克风 200ms（短时回声）。"""
    segs = [Speech(REQUEST), AfterEvent("playback_started", 1000), Echo(200)]
    return Scenario(name="F", segments=segs, llm_replies={}, default_reply=LONG_REPLY,
                    llm_faults=LLM, tts_faults=TTS, close_after=("context_committed", 2, SETTLE_MS))
