"""RuntimeConfig：所有默认参数集中在这里（设计 §16）。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeConfig:
    # VAD 与判停
    vad_start_frames: int = 3
    vad_end_frames: int = 4
    initial_silence_ms: int = 200
    final_wait_ms: int = 300
    max_silence_ms: int = 1500
    l1_budget_ms: int = 50
    l2_budget_ms: int = 150
    l3_budget_ms: int = 500
    high_start: float = 0.85
    low: float = 0.3
    min_conf: float = 0.5
    high_floor: float = 0.5
    threshold_floor_at_ms: int = 1000
    speculate_threshold: float = 0.6
    speculation_enabled: bool = True
    # 打断门禁
    pause_min_speech_frames: int = 3
    noise_timeout_ms: int = 400
    backchannel_budget_ms: int = 100
    backchannel_threshold: float = 0.5
    # 超时与关闭
    llm_first_timeout_ms: int = 2000
    tts_first_timeout_ms: int = 1000
    inter_chunk_timeout_ms: int = 1000
    cancel_grace_ms: int = 1000
    close_grace_ms: int = 2000
    # 容量
    asr_in_capacity: int = 50
    text_capacity: int = 64
    playback_capacity_frames: int = 75
    context_max_messages: int = 20
    # 其他
    progress_interval_ms: int = 100
    l2_audio_ring_ms: int = 3000
    backchannel_audio_max_ms: int = 2000
    max_trim_ms: int = 300
    trim_enabled: bool = True
    log_audio_frames: bool = True
