# 一次 barge-in 的 trace 分析（场景 C）

只依据 [`barge_in.trace.jsonl`](barge_in.trace.jsonl)（422 行，虚拟时钟，单位 ms）。下文 `#n` 指 `sequence_number`，`@t` 指 `timestamp`。`script_marker` 是脚本写入的评测真值（`eval_only=true`），Runtime 不读取，只用来度量。

## 打断前：第一轮回复在播

- `#57 @740` `endpoint_committed`（turn 1，gen 2）。gen 1 是被 `#47` 作废的预生成（文本还缺 "please"），`#58` 采用了 gen 2 这个预生成。
- `#87 @1200` gen 2 `playback_started`。

## 打断过程

| 步骤 | 事件 | 说明 |
|---|---|---|
| 开口 | `#196 @2400` `script_marker(speech start)` "Stop. Make it Friday instead." | 真实开口时刻，距播放开始正好 1200ms |
| 检测 | `#203 @2440` `speech_start(epoch=2, ts_ms=2400)` | 连续 3 帧语音（2400/2420/2440）确认，检测延迟 40ms |
| 暂停 | `#205 @2440` `playback_stopped(paused_pending_decision, position_samples=20160)` | 同一 tick 暂停，停播延迟 40ms（目标 ≤ 250ms）。20160 样本 = 1260ms；`#204` 补记了最后一段进度 `[2, 60, 62]` |
| 暂停中 | `#217 @2605` gen 2 的 `tts_chunk 10` 仍在产出（`#218` 入队后 depth=65） | 暂停不取消上游，TTS 继续往 playback 队列写，但门控是 paused，一帧都没播 |
| 识别 | `#220 @2620` `asr_partial "Stop"` | "Stop" 在 2500 说完，加上 partial 延迟后到达 |
| 判定 | `#222 @2640` `bargein_decision(result=interrupt, rule=command_word, words=["stop"], utterance_id=1)` | 指令词直接判为打断，不等整句说完。判定延迟 240ms |
| 停止 + 已听文本 | `#223 @2640` `playback_stopped(interrupted)` | `playback_truth`：已播 1260ms，`heard_text` = "Sure, I can help with that. Your table for two is booked for"。chunk 5（" for Thursday"）只播了 2560/3520 样本，"Thursday" 播到一半，按"半个词算没听到"处理；chunk 6～10 已入队但未播 |
| 作废 | `#224 @2640` `generation_cancelled(gen 2, reason=barge_in)` | |
| purge | `#225 @2640` `stale_event_dropped(gate=purge, frames=65)` | 同步清掉 gen 2 留在 playback 队列里的 65 帧 |
| 上下文 | `#226`、`#227` `context_committed` | 旧一轮的用户文本，以及助手**实际听到**的部分（`interrupted=true`），不是完整生成文本 |
| 迟到项 | `#228 @2640` `stale_event_dropped(gate=2, phase=late, unit=eos)` | cancel 之后 gen 2 的 TTS 流才结束，它的 EOS 在闸②被拦下。本场景的假 TTS 不吐迟到 chunk（那是场景 E），所以 `stale_chunk_received=0` |

## 新一轮

- 从 `#229` 起事件的 `turn_id=2`：打断用的这句话成为新一轮的用户输入，之后的 partial（`#233` "Stop Make" 等）进入判停。
- `#244 @2920` 用户说完；`#250 @2980` `speech_end(ts_ms=2920)`。
- `#252`、`#260` 先后起了预生成 gen 3、gen 4（gen 3 因文本变化在 `#258` 被作废）。
- `#265 @3100` `asr_final "Stop. Make it Friday instead."`，`#266` L1 给出 p=0.95。
- `#267 @3120` `endpoint_committed`（gen 4，阈值 0.85），endpoint latency = 3120 − 2920 = 200ms；`#268` 采用 gen 4。
- `#299 @3580` gen 4 `playback_started`，turn-end → first-audio = 460ms。
- `#402 @4920` `playback_stopped(completed)`，播放 1340ms；`#404` 助手文本 "OK, Friday it is. ..." 完整进入上下文。
- `#421 @5220` `session_closed`：`overdue_task_count=0`，playback 队列最大深度 65（容量 75）。

## 结论

- 开口 → 停声 40ms，开口 → 判定 240ms，overlap 40ms（开口到暂停之间双方同时出声）。
- 停声靠暂停，不靠 cancel。判定前上游仍在生产（`#217`），判定后才作废并 purge。
- gen 2 最后一次帧写入记在 `#204`，早于 `generation_cancelled`（`#224`），所以 `stale_chunk_played=0`。
- 下一轮上下文里的助手文本只有听到的 60 个字符。
