# 各场景指标汇总

单位：ms（计数类除外）；列表表示该场景内每次发生的值，`-` 表示未发生。

| 指标 | A_prime | A | B | C | D | E |
|---|---|---|---|---|---|---|
| endpoint_latency_ms | 200 | 200 | 200 | 200, 200 | 200 | 200, 200 |
| turn_end_to_first_audio_ms | 540 | 460 | 460 | 460, 460 | 460 | 540, 540 |
| speech_start_detection_ms | - | - | - | 40 | - | 40 |
| playback_stop_ms | - | - | - | 40 | - | 40 |
| interruption_decision_ms | - | - | - | 240 | - | 240 |
| overlap_ms | - | - | - | 40 | - | 40 |
| false_interruption_count | 0 | 0 | 0 | 0 | 0 | 0 |
| false_pause_count | 0 | 0 | 0 | 0 | 1 | 0 |
| pause_total_ms | 0 | 0 | 0 | 0 | 240 | 0 |
| stale_chunk_received | 0 | 0 | 0 | 0 | 0 | 2 |
| stale_chunk_played | 0 | 0 | 0 | 0 | 0 | 0 |
| stale_purged_frames | 0 | 0 | 32 | 65 | 0 | 65 |
| stale_rejected_on_wake | 0 | 0 | 0 | 0 | 0 | 0 |
| queue_max_depth | asr_in=1<br>playback=25<br>text=2 | asr_in=1<br>playback=25<br>text=2 | asr_in=1<br>playback=32<br>text=2 | asr_in=1<br>playback=65<br>text=2 | asr_in=1<br>playback=75<br>text=3 | asr_in=1<br>playback=65<br>text=2 |
