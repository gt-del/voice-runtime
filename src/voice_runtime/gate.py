"""BargeInGate 判定逻辑（设计 §9.1、§9.4）：纯函数式规则，不含状态机与播放控制。"""
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from voice_runtime.audio import SAMPLE_RATE
from voice_runtime.config import RuntimeConfig
from voice_runtime.providers.base import BackchannelScorer
from voice_runtime.text import normalize_text

COMMAND_WORDS = ("stop", "wait", "no", "hold on")
BACKCHANNELS = ("uh-huh", "mm-hmm", "yeah", "yep", "right", "okay", "sure", "i see", "got it")

SLICE_BYTES = SAMPLE_RATE * 80 // 1000 * 2  # 80ms PCM16 = 1280 samples = 2560 字节
# 回声比对窗口：机器人在 [开口前 500ms, 这段话结束（未结束则为现在）+ 100ms) 内实际播出的词
ECHO_LOOKBACK_MS = 500
ECHO_LAG_MS = 100

_COMMANDS = tuple(tuple(normalize_text(c).split()) for c in COMMAND_WORDS)
_BACKCHANNELS = tuple(tuple(normalize_text(b).split()) for b in BACKCHANNELS)


class GateResult(StrEnum):
    PENDING = "pending"
    INTERRUPT = "interrupt"
    BACKCHANNEL = "backchannel"
    NOISE = "noise"
    ECHO = "echo"


@dataclass
class Utterance:
    utterance_id: int
    start_ms: int
    vad_ended_at_ms: int | None
    partial_text: str
    final_text: str | None
    audio: bytearray

    def append_audio(self, pcm: bytes, cfg: RuntimeConfig) -> None:
        """追加 PCM，累计不超过 backchannel_audio_max_ms，超出部分丢弃。"""
        cap = SAMPLE_RATE * 2 * cfg.backchannel_audio_max_ms // 1000
        room = cap - len(self.audio)
        if room > 0:
            self.audio += pcm[:room]


def _split_echo(tokens: list[str], played: list[str]) -> tuple[list[str], list[str]]:
    """按顺序与播放窗口比对：能依次在窗口中找到的 token 是回声，其余是用户的词。返回 (用户词, 回声词)。"""
    user, echo, j = [], [], 0
    for t in tokens:
        if t in played[j:]:
            j = played.index(t, j) + 1
            echo.append(t)
        else:
            user.append(t)
    return user, echo


def _has_command(tokens: list[str]) -> bool:
    for cmd in _COMMANDS:
        n = len(cmd)
        if any(tuple(tokens[i:i + n]) == cmd for i in range(len(tokens) - n + 1)):
            return True
    return False


def _is_pure_backchannel(tokens: list[str]) -> bool:
    """tokens 能否被完整切分为附和词组。"""
    ok = [False] * (len(tokens) + 1)
    ok[0] = True
    for i in range(1, len(tokens) + 1):
        ok[i] = any(i >= len(p) and ok[i - len(p)] and tuple(tokens[i - len(p):i]) == p
                    for p in _BACKCHANNELS)
    return bool(tokens) and ok[-1]


def _is_backchannel_compatible(tokens: list[str]) -> bool:
    """tokens 可切分为附和词组，且最后一段允许是某个词组的前缀（说到一半）。"""
    for j in range(len(tokens) + 1):
        if not (j == 0 or _is_pure_backchannel(tokens[:j])):
            continue
        rest = tuple(tokens[j:])
        if not rest or any(p[:len(rest)] == rest for p in _BACKCHANNELS):
            return True
    return False


async def decide(u: Utterance, now_ms: int, cfg: RuntimeConfig, scorer: BackchannelScorer,
                 played: Sequence[str] = ()) -> tuple[GateResult, str, dict]:
    """played：机器人最近实际播出的词（Playback Truth）。与之按顺序吻合的词是回声，不算用户的词。"""
    window = normalize_text(" ".join(played)).split()
    has_final = u.final_text is not None
    final_all = normalize_text(u.final_text).split() if has_final else []
    final_tokens, final_echo = _split_echo(final_all, window)
    partial_tokens, partial_echo = _split_echo(normalize_text(u.partial_text).split(), window)
    tokens = final_tokens if has_final else partial_tokens
    details: dict = {"words": tokens, "echo_words": final_echo if has_final else partial_echo,
                     "scores": None}

    # 1. 指令词（partial 或 final 中的完整词；回声里的指令词不算）
    if _has_command(partial_tokens) or _has_command(final_tokens):
        return GateResult.INTERRUPT, "command_word", details

    # 2. 噪声（先于搭腔，空文本永远不会被判为搭腔）
    if has_final and not final_all:
        return GateResult.NOISE, "noise_empty_final", details
    # 回声：final 的词全部与播放窗口吻合（§9.4）
    if has_final and not final_tokens:
        return GateResult.ECHO, "echo_match", details
    if not has_final and u.vad_ended_at_ms is not None:
        elapsed = now_ms - u.vad_ended_at_ms
        if elapsed >= cfg.noise_timeout_ms:
            details["elapsed_ms"] = elapsed
            return GateResult.NOISE, "noise_timeout", details

    if has_final:
        # 3. 搭腔
        if _is_pure_backchannel(final_tokens):
            slices = [bytes(u.audio[i:i + SLICE_BYTES]) for i in range(0, len(u.audio), SLICE_BYTES)]
            details["scorer"] = "ok"
            try:
                async with asyncio.timeout(cfg.backchannel_budget_ms / 1000):
                    scores = await scorer.score(slices)
                details["scores"] = list(scores)
                if scores and max(scores) < cfg.backchannel_threshold:
                    return GateResult.INTERRUPT, "default_interrupt", details
            except TimeoutError:
                details["scorer"] = "timeout"  # 评分超时：视为满足
            except Exception:  # CancelledError 是 BaseException，照常传播
                details["scorer"] = "error"  # 评分失败：视为满足
            return GateResult.BACKCHANNEL, "backchannel", details
        # 4. 已有 final → 默认打断
        return GateResult.INTERRUPT, "default_interrupt", details

    # 4. partial 提前默认打断
    if len(partial_tokens) >= 2 and not _is_backchannel_compatible(partial_tokens):
        return GateResult.INTERRUPT, "default_interrupt", details
    return GateResult.PENDING, "pending", details
