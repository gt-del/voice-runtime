"""指标：只从事件日志离线计算（设计 §9.2、§11.4）。"""
from voice_runtime.events import (BARGEIN_DECISION, ENDPOINT_COMMITTED, GENERATION_CANCELLED,
                                  PLAYBACK_PROGRESS, PLAYBACK_RESUMED, PLAYBACK_STARTED,
                                  PLAYBACK_STOPPED, SCRIPT_MARKER, SESSION_CLOSED, SPEECH_START,
                                  STALE_EVENT_DROPPED)

PAUSED = "paused_pending_decision"


def _first_after(events: list[dict], i: int, pred) -> dict | None:
    return next((e for e in events[i + 1:] if pred(e)), None)


def _is_marker(e: dict, segment: str, phase: str) -> bool:
    return (e["event_type"] == SCRIPT_MARKER and e["payload"].get("segment") == segment
            and e["payload"].get("phase") == phase)


def compute_metrics(events: list[dict]) -> dict:
    events = sorted(events, key=lambda e: e["sequence_number"])
    m: dict = {
        "endpoint_latency_ms": [], "turn_end_to_first_audio_ms": [],
        "speech_start_detection_ms": [], "playback_stop_ms": [],
        "interruption_decision_ms": [], "overlap_ms": [],
        "false_interruption_count": 0, "false_pause_count": 0, "pause_total_ms": 0,
        "stale_chunk_received": 0, "stale_chunk_played": 0, "stale_purged_frames": 0,
        "stale_rejected_on_wake": 0, "queue_max_depth": {},
    }
    last_speech_end: int | None = None
    max_gen = 0  # 目前出现过的最大 generation_id
    playing: set[int] = set()  # 已 playback_started、尚未终止的 generation（含暂停中）
    audible = False  # 此刻是否有声音在播（暂停时为 False）
    last_utterance: str | None = None  # 最近一个 speech / noise 段的类型
    cancelled_at: dict[int, int] = {}  # generation → 非 duplicate 的 generation_cancelled 序号

    for i, e in enumerate(events):
        et, p, ts = e["event_type"], e["payload"], e["timestamp"]
        if et == SCRIPT_MARKER:
            if _is_marker(e, "speech", "end"):
                last_speech_end = ts
            if p.get("phase") == "start" and p.get("segment") in ("speech", "noise"):
                last_utterance = p["segment"]
            if _is_marker(e, "speech", "start") and playing:
                _barge_in_onset(events, i, audible, m)
        elif et == ENDPOINT_COMMITTED:
            if last_speech_end is not None:
                m["endpoint_latency_ms"].append(ts - last_speech_end)
            gid = e["generation_id"]  # 服务本次判停的 generation（采用的预生成或新起的）
            if gid is not None:
                first = _first_after(events, i, lambda x: x["event_type"] == PLAYBACK_STARTED
                                     and x["generation_id"] == gid)
            else:  # 旧日志没有 generation_id：取之后第一个更新的 generation
                floor = max_gen
                first = _first_after(events, i, lambda x: x["event_type"] == PLAYBACK_STARTED
                                     and (x["generation_id"] or 0) > floor)
            if first is not None:
                m["turn_end_to_first_audio_ms"].append(first["timestamp"] - ts)
        elif et == PLAYBACK_STARTED:
            playing.add(e["generation_id"])
            audible = True
        elif et == PLAYBACK_STOPPED:
            audible = False
            if p.get("reason") != PAUSED:
                playing.discard(e["generation_id"])
        elif et == PLAYBACK_RESUMED:
            audible = True
            m["false_pause_count"] += 1
            m["pause_total_ms"] += p.get("paused_ms", 0)
        elif et == BARGEIN_DECISION:
            if p.get("result") == "interrupt" and last_utterance == "noise":
                m["false_interruption_count"] += 1
        elif et == GENERATION_CANCELLED:
            if not p.get("duplicate") and e["generation_id"] not in cancelled_at:
                cancelled_at[e["generation_id"]] = e["sequence_number"]
        elif et == STALE_EVENT_DROPPED:
            phase = p.get("phase")
            if phase == "late" and p.get("unit") == "chunk":
                m["stale_chunk_received"] += 1
            elif phase == "purge":
                m["stale_purged_frames"] += p.get("frames", 0)
            elif phase == "rejected_on_wake":
                m["stale_rejected_on_wake"] += 1
        elif et == PLAYBACK_PROGRESS:
            for gen, first_frame, last_frame in p.get("ranges", []):
                c = cancelled_at.get(gen)
                if c is not None and c < e["sequence_number"]:
                    m["stale_chunk_played"] += last_frame - first_frame + 1
        elif et == SESSION_CLOSED:
            m["queue_max_depth"] = dict(p.get("queue_max_depth", {}))
        if e["generation_id"] is not None:
            max_gen = max(max_gen, e["generation_id"])
    return m


def _barge_in_onset(events: list[dict], i: int, audible: bool, m: dict) -> None:
    """events[i] 是播放期间的一个 speech 开口：三段延迟与 overlap（设计 §9.2）。"""
    onset = events[i]["timestamp"]
    start = _first_after(events, i, lambda x: x["event_type"] == SPEECH_START)
    if start is not None:
        m["speech_start_detection_ms"].append(start["timestamp"] - onset)
    # 停播只与本段配对：须在本段结束（end marker）之前、且在下一个 speech/noise 段开始之前
    end = _first_after(events, i, lambda x: _is_marker(x, "speech", "end"))
    nxt = _first_after(events, i, lambda x: x["event_type"] == SCRIPT_MARKER
                       and x["payload"].get("phase") == "start"
                       and x["payload"].get("segment") in ("speech", "noise"))
    limit = min(x["sequence_number"] for x in (end, nxt, events[-1]) if x is not None)
    stop = _first_after(events, i, lambda x: x["event_type"] == PLAYBACK_STOPPED
                        and x["payload"].get("reason") == PAUSED)
    if stop is not None and stop["sequence_number"] > limit:
        stop = None
    if stop is not None:
        m["playback_stop_ms"].append(stop["timestamp"] - onset)
    decision = _first_after(events, i, lambda x: x["event_type"] == BARGEIN_DECISION
                            and x["payload"].get("result") == "interrupt")
    if decision is not None:
        m["interruption_decision_ms"].append(decision["timestamp"] - onset)
    if not audible:
        m["overlap_ms"].append(0)
        return
    silenced = _first_after(events, i, lambda x: x["event_type"] == PLAYBACK_STOPPED)  # 任意原因停声
    ends = [x["timestamp"] for x in (end, silenced) if x is not None]
    m["overlap_ms"].append(min(ends) - onset if ends else 0)
