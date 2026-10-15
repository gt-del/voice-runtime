"""投机预生成（设计 §7、§8.3、§8.4）：启动、采用、作废、held 音频上限、关闭时不遗留。"""
from dataclasses import replace

from helpers import run_scenario

from voice_runtime.providers.fake import StreamFaults, normalize_text
from voice_runtime.scenarios import LONG_REPLY, scenario_a
from voice_runtime.script import Silence, Speech


def of(events, event_type, **match):
    return [e for e in events if e["event_type"] == event_type
            and all(e["payload"].get(k) == v for k, v in match.items())]


def _discard_scenario():
    # "Book a table" 后停 400ms：L1 给 0.7 ≥ 0.6，开始预生成；此时阈值还在 0.7 以上，不会判停。
    # 再说 "for"：文本变成以功能词结尾（p=0.1，不重启预生成），最后靠 max_silence 判停，文本不同 → 作废。
    # 用约 4s 的长回复，预生成 held 的近 2s 里足以把 playback 队列灌满
    return replace(scenario_a(), name="discard", default_reply=LONG_REPLY,
                   segments=[Speech("Book a table"), Silence(400), Speech("for")])


def test_speculation_discarded_when_text_changes(tmp_path):
    r = run_scenario(_discard_scenario(), tmp_path)
    ev = r.events
    started = of(ev, "speculation_started")
    assert len(started) == 1 and started[0]["payload"]["text"] == "Book a table"
    spec_gid = started[0]["generation_id"]
    disc = of(ev, "speculation_discarded")
    assert len(disc) == 1 and disc[0]["generation_id"] == spec_gid
    assert disc[0]["payload"]["spec_text"] == "Book a table"
    assert disc[0]["payload"]["final_text"] == "Book a table for"
    assert not of(ev, "speculation_adopted")
    cancel = of(ev, "generation_cancelled", reason="speculation_discarded", duplicate=False)
    assert [c["generation_id"] for c in cancel] == [spec_gid]
    # 判停文本起了新的 generation，endpoint_committed 指向它，并由它出声
    commit = of(ev, "endpoint_committed")[0]
    new_gid = commit["generation_id"]
    assert new_gid > spec_gid and commit["payload"]["text"] == "Book a table for"
    assert [e["generation_id"] for e in of(ev, "playback_started")] == [new_gid]
    # 预生成确实已有音频入队（被 held 拦着），作废时被清掉，sink 中没有它的任何帧
    assert of(ev, "audio_enqueued") and of(ev, "audio_enqueued")[0]["generation_id"] == spec_gid
    purge = [p for p in of(ev, "stale_event_dropped", phase="purge")
             if p["generation_id"] == spec_gid]
    assert purge and purge[0]["payload"]["frames"] > 0
    assert r.sink.writes and not [w for w in r.sink.writes if w[1] == spec_gid]
    assert r.metrics["stale_chunk_played"] == 0
    # 被作废的预生成文字不进上下文
    assert [m["content"] for m in r.session.context if m["role"] == "user"] == ["Book a table for"]


def test_speculation_held_audio_never_exceeds_playback_capacity(tmp_path):
    r = run_scenario(_discard_scenario(), tmp_path)
    cap = r.session.cfg.playback_capacity_frames
    assert cap == 75
    # 预生成 held 了近 2s：上限就是 playback 队列容量（约 1.5s 音频），满了 TTS 被背压阻塞
    assert r.metrics["queue_max_depth"]["playback"] == cap
    ra = run_scenario(scenario_a(), tmp_path)
    assert 0 < ra.metrics["queue_max_depth"]["playback"] <= cap


def test_speculation_not_restarted_for_same_text(tmp_path):
    r = run_scenario(scenario_a(), tmp_path)
    ev = r.events
    th = r.session.cfg.speculate_threshold
    # 场景 A 里 ASR 比 VAD 慢：静音中依次到达 "...for two"、"...please"（partial）、"...please."（final）
    final = of(ev, "asr_final")[-1]
    last_partial = of(ev, "asr_partial")[-1]
    assert normalize_text(final["payload"]["text"]) == normalize_text(last_partial["payload"]["text"])
    # final 到达后 L1 重新打了分且过了阈值，但规范化文本没变：不重启
    rescored = [e for e in of(ev, "turn_score") if e["payload"]["level"] == 1
                and e["payload"]["p"] >= th and e["sequence_number"] > final["sequence_number"]]
    assert rescored
    started = of(ev, "speculation_started")
    assert all(s["sequence_number"] < final["sequence_number"] for s in started)
    texts = [normalize_text(s["payload"]["text"]) for s in started]
    assert len(texts) == len(set(texts))  # 每次重启都是因为文本真的变了
    assert [c["payload"]["reason"] for c in of(ev, "generation_cancelled")] == \
        ["speculation_superseded"] * (len(started) - 1)
    assert of(ev, "speculation_adopted")[0]["generation_id"] == started[-1]["generation_id"]


def test_speculation_superseded_when_text_changes_in_silence(tmp_path):
    segs = [Speech("Book a table"), Silence(400), Speech("for two please.")]
    r = run_scenario(replace(scenario_a(), name="supersede", segments=segs), tmp_path)
    ev = r.events
    started = of(ev, "speculation_started")
    texts = [s["payload"]["text"] for s in started]
    assert texts[0] == "Book a table" and texts[-1] == "Book a table for two please"
    old = [s["generation_id"] for s in started[:-1]]
    last = started[-1]["generation_id"]
    sup = of(ev, "generation_cancelled", reason="speculation_superseded", duplicate=False)
    assert [c["generation_id"] for c in sup] == old
    assert [a["generation_id"] for a in of(ev, "speculation_adopted")] == [last]
    assert of(ev, "endpoint_committed")[0]["generation_id"] == last
    assert {w[1] for w in r.sink.writes} == {last}


def test_close_during_speculation_leaves_nothing_behind(tmp_path):
    # 预生成的音频已入队（held）、尚未判停时关闭
    sc = replace(_discard_scenario(), close_after=("audio_enqueued", 1, 0))
    r = run_scenario(sc, tmp_path)
    ev = r.events
    spec_gid = of(ev, "speculation_started")[0]["generation_id"]
    assert not of(ev, "endpoint_committed")
    cancel = of(ev, "generation_cancelled", duplicate=False)
    assert [(c["generation_id"], c["payload"]["reason"]) for c in cancel] == \
        [(spec_gid, "session_closed")]
    assert not r.sink.writes
    s = r.session
    assert s._gens == {} and s._sups == {} and s._running_sups == set()
    assert s.active_generation_id is None


def test_speculation_failure_does_not_end_turn(tmp_path):
    # LLM 首包卡死、首包超时调到 1000ms：预生成在静音中（判停前）就超时失败
    sc = replace(_discard_scenario(), llm_faults=StreamFaults(300, 50, hang_after=0),
                 close_after=("generation_failed", 2, 100))
    r = run_scenario(sc, tmp_path, llm_first_timeout_ms=1000)
    ev = r.events
    spec_gid = of(ev, "speculation_started")[0]["generation_id"]
    failed = of(ev, "generation_failed")
    commit = of(ev, "endpoint_committed")[0]
    assert failed[0]["generation_id"] == spec_gid
    assert failed[0]["sequence_number"] < commit["sequence_number"]
    assert not [p for p in of(ev, "playback_stopped") if p["generation_id"] == spec_gid]
    # 轮次没有因为预生成失败而结束：判停前没有 context_committed，判停文本仍是整段话
    assert not [c for c in of(ev, "context_committed")
                if c["sequence_number"] < commit["sequence_number"]]
    assert commit["payload"]["text"] == "Book a table for" and commit["turn_id"] == 1
    # 预生成已不在：判停时不比较、不写 discarded，直接新起 generation
    assert not of(ev, "speculation_discarded") and not of(ev, "speculation_adopted")
    assert commit["generation_id"] > spec_gid
    assert failed[1]["generation_id"] == commit["generation_id"]
