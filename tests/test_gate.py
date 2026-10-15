import asyncio

import pytest

from voice_runtime.config import RuntimeConfig
from voice_runtime.gate import GateResult, Utterance, decide
from voice_runtime.providers.fake import FakeBackchannelScorer
from voice_runtime.vloop import run

CFG = RuntimeConfig()
SLICE = 2560  # 80ms @16kHz PCM16


def utt(partial="", final=None, vad_end=None, audio=SLICE):
    u = Utterance(utterance_id=1, start_ms=0, vad_ended_at_ms=vad_end,
                  partial_text=partial, final_text=final, audio=bytearray(audio))
    return u


def decide_sync(u, now, scorer=None, cfg=CFG):
    return run(decide(u, now, cfg, scorer or FakeBackchannelScorer()))


@pytest.mark.parametrize("partial,final,vad_end,now,exp_result,exp_rule", [
    ("stop", None, None, 100, "interrupt", "command_word"),
    ("hold on", None, None, 100, "interrupt", "command_word"),
    ("stopping", None, None, 100, "pending", "pending"),
    ("", "", 160, 320, "noise", "noise_empty_final"),
    ("", None, 160, 561, "noise", "noise_timeout"),
    ("yeah", "Yeah.", 300, 450, "backchannel", "backchannel"),
    ("i", None, None, 100, "pending", "pending"),
    ("i see", None, None, 100, "pending", "pending"),
    ("make it", None, None, 100, "interrupt", "default_interrupt"),
    ("okay but", "Okay, but make it Friday.", 900, 1050, "interrupt", "default_interrupt"),
    ("", None, 160, 400, "pending", "pending"),
    ("", None, None, 5000, "pending", "pending"),
])
def test_decision_table(partial, final, vad_end, now, exp_result, exp_rule):
    result, rule, details = decide_sync(utt(partial, final, vad_end), now)
    assert (result, rule) == (GateResult(exp_result), exp_rule)
    assert "words" in details and "scores" in details


def test_details_words_and_noise_elapsed():
    _, _, d = decide_sync(utt("", None, 160), 561)
    assert d["words"] == [] and d["scores"] is None and d["elapsed_ms"] == 401
    _, _, d = decide_sync(utt("uh-huh", "Uh-huh.", 100), 200)
    assert d["words"] == ["uhhuh"] and d["scores"] == [0.8]


def test_multiword_backchannel_phrases():
    for final in ["okay sure", "I see.", "Got it, yeah"]:
        assert decide_sync(utt(final, final, 100), 200)[1] == "backchannel"
    # 非纯附和：含其他词
    assert decide_sync(utt("yeah no way", "yeah no way", 100), 200)[1] == "command_word"
    assert decide_sync(utt("yeah right now", "yeah right now", 100), 200)[1] == "default_interrupt"


def test_backchannel_requires_max_slice_score_at_least_threshold():
    sc = FakeBackchannelScorer(by_slice_count=lambda n: [0.1, 0.3])
    r, rule, d = decide_sync(utt("yeah", "Yeah.", 100, audio=2 * SLICE), 200, sc)
    assert (r, rule, d["scores"]) == (GateResult.INTERRUPT, "default_interrupt", [0.1, 0.3])
    sc = FakeBackchannelScorer(by_slice_count=lambda n: [0.1, 0.6])
    r, rule, _ = decide_sync(utt("yeah", "Yeah.", 100, audio=2 * SLICE), 200, sc)
    assert (r, rule) == (GateResult.BACKCHANNEL, "backchannel")


def test_trailing_partial_slice_counts_as_one():
    seen = []

    async def go():
        class S:
            async def score(self, slices):
                seen.append(len(slices))
                return [0.9] * len(slices)
        await decide(utt("yeah", "yeah", 100, audio=2 * SLICE + 1), 200, CFG, S())
    run(go())
    assert seen == [3]


def test_backchannel_scorer_timeout_counts_as_backchannel():
    sc = FakeBackchannelScorer(delay_ms=CFG.backchannel_budget_ms + 50)
    assert decide_sync(utt("yeah", "Yeah.", 100), 200, sc)[:2] == (GateResult.BACKCHANNEL, "backchannel")


def test_backchannel_scorer_failure_counts_as_backchannel():
    sc = FakeBackchannelScorer(fail=True)
    assert decide_sync(utt("yeah", "Yeah.", 100), 200, sc)[:2] == (GateResult.BACKCHANNEL, "backchannel")


def test_scorer_cancellation_propagates():
    async def go():
        t = asyncio.create_task(decide(utt("yeah", "yeah", 100), 200, CFG,
                                       FakeBackchannelScorer(delay_ms=50)))
        await asyncio.sleep(0.01)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
    run(go())


def test_utterance_audio_capped():
    u = utt()
    u.audio.clear()
    cap = 16000 * 2 * CFG.backchannel_audio_max_ms // 1000
    for _ in range(10):
        u.append_audio(bytes(cap // 4 + 1), CFG)
    assert len(u.audio) == cap
    u.append_audio(b"\x01" * 100, CFG)
    assert len(u.audio) == cap


def test_noise_timeout_inequality_holds_for_defaults():
    c = RuntimeConfig()
    assert c.noise_timeout_ms > 150 + 20 + 20  # final_latency + 最大抖动 + 一帧 asr_in 排队延迟


@pytest.mark.parametrize("partial", ["okay sure", "got it yeah", "yeah yeah", "i see yeah", "got", "yeah i"])
def test_multi_backchannel_partials_stay_pending(partial):
    assert decide_sync(utt(partial), 100)[:2] == (GateResult.PENDING, "pending")


@pytest.mark.parametrize("partial", ["make it", "okay but", "yeah make it"])
def test_non_backchannel_partials_interrupt(partial):
    assert decide_sync(utt(partial), 100)[:2] == (GateResult.INTERRUPT, "default_interrupt")


def test_scorer_detail_distinguishes_fallback():
    f = lambda sc: decide_sync(utt("yeah", "Yeah.", 100), 200, sc)[2]["scorer"]  # noqa: E731
    assert f(FakeBackchannelScorer()) == "ok"
    assert f(FakeBackchannelScorer(delay_ms=CFG.backchannel_budget_ms + 50)) == "timeout"
    assert f(FakeBackchannelScorer(fail=True)) == "error"


PLAYED = ["booked", "for", "Thursday", "at"]  # 机器人这段时间实际播出的词（Playback Truth）


def decide_played(u, now, played=PLAYED):
    return run(decide(u, now, CFG, FakeBackchannelScorer(), played))


def test_all_echo_final_is_echo():
    r, rule, d = decide_played(utt("for Thursday", "for Thursday", 300), 450)
    assert (r, rule) == (GateResult.ECHO, "echo_match")
    assert d["words"] == [] and d["echo_words"] == ["for", "thursday"]


def test_echo_plus_user_words_decided_on_user_words():
    r, rule, d = decide_played(utt("Thursday make it", "Thursday make it Friday.", 300), 450)
    assert (r, rule) == (GateResult.INTERRUPT, "default_interrupt")
    assert d["words"] == ["make", "it", "friday"] and d["echo_words"] == ["thursday"]
    r, rule, d = decide_played(utt("Thursday yeah", "Thursday, yeah.", 300), 450)
    assert (r, rule, d["words"]) == (GateResult.BACKCHANNEL, "backchannel", ["yeah"])


def test_echo_tokens_in_partial_do_not_trigger_early_interrupt():
    r, rule, d = decide_played(utt("Thursday at"), 100)
    assert (r, rule) == (GateResult.PENDING, "pending")
    assert d["words"] == [] and d["echo_words"] == ["thursday", "at"]


def test_echoed_command_word_does_not_interrupt():
    played = ["Please", "stop", "here."]
    assert decide_played(utt("stop"), 100, played)[:2] == (GateResult.PENDING, "pending")
    assert decide_played(utt("stop", "Stop.", 300), 450, played)[:2] == \
        (GateResult.ECHO, "echo_match")
    # 用户自己说的 stop（不在播放窗口里）照常打断
    assert decide_played(utt("stop"), 100)[:2] == (GateResult.INTERRUPT, "command_word")


def test_echo_match_follows_played_order():
    # 窗口里是 "for Thursday"：倒序的 "Thursday for" 只有第一个词能按顺序对上
    r, _, d = decide_played(utt("Thursday for", "Thursday for", 300), 450)
    assert d["echo_words"] == ["thursday"] and d["words"] == ["for"] and r is GateResult.INTERRUPT


def test_empty_final_stays_noise_with_played_window():
    assert decide_played(utt("", "", 160), 320)[:2] == (GateResult.NOISE, "noise_empty_final")
