# DESIGN：最小 Voice Runtime

> 由完整设计文档 [docs/superpowers/specs/2026-10-15-voice-runtime-design.md](docs/superpowers/specs/2026-10-15-voice-runtime-design.md) 压缩而来，并按实际实现修正。文中数字均来自 [sample-output/metrics.json](sample-output/metrics.json) 和对应 trace（虚拟时钟，单位 ms）。

## 1. 架构

```
 ScriptedAudioSource ──20ms 帧──► Session（唯一 owner：ID、状态机、TaskGroup、active_generation_id）
   │ script_marker（评测真值，        │
   │ Runtime 不读）                   ├─► VAD（逐帧同步）─► SpeechTracker（speech_start / speech_end、epoch）
   │                                  ├─► [asr_in 50 帧，满则丢最旧] ─► ASR（session 级长流）─► partial / final
   │                                  ├─► EndpointCascade：L1 规则 ─► L2 ─► L3 ─► 静音硬上限
   │                                  │        └─► 投机预生成（同一时间至多 1 个）
   │                                  ├─► BargeInGate（播放期间：暂停 → 判定 → 恢复 / 打断）
   │                                  └─► GenerationSupervisor(gen_id)
   │                                        LLM ─闸①─► [text 64，阻塞] ─► TTS ─裁剪─闸②─► [playback 75 帧，阻塞]
   │                                                                                          │
   │                                                       Player（门控 + 闸③，每 20ms 取 1 帧）─► AudioSink
   └──────────── 所有组件 ──同步写──► EventLog（JSONL）──离线──► compute_metrics
```

Session 是中心协调者：启动 / 作废 generation、切换 `active_generation_id`、改变播放器门控、关闭 session，都只有它能做。各阶段之间用有界队列流式衔接。

**为什么用虚拟时钟**：自定义 event loop 重写 `time()`，没有就绪回调时直接跳到下一个定时器。`asyncio.sleep` / `timeout` 自动走虚拟时间，没有代码能绕过时钟。好处是"700ms 停顿""250ms 停播"这类断言每次结果一致，测试不用真等，trace 逐字节可复现（有测试断言；`run_scenarios.py` 重跑后 `git diff` 为空）。代价是核心代码不能用线程或真实 I/O 等待（有测试 grep 禁止），真实模型适配器因此不能直接接进来（见 §8）。

## 2. 关键状态与事件

**Session 状态**：`LISTENING`（收音、判停、可预生成）→ 判停 → `PENDING`（已判停、还没出声）→ 首帧播出 → `PLAYING` ⇄ `PAUSED`（门禁暂停 / 恢复）→ 播完、截断播完、失败或被打断 → `LISTENING`；任意状态 → `CLOSING` → `CLOSED`。

**播放器门控**：`idle / held / playing / paused / closed` 一套机制。预生成未放行时是 `held`，门禁暂停时是 `paused`；只有 `playing` 时才取帧，所以门控一变，20ms 内就没有声音。

**ID**：`session_id`（构造时）；`turn_id`（LISTENING 下第一个 `speech_start`，或门禁确认打断时 +1）；`generation_id`（每次启动 LLM→TTS，包括预生成，全 session 单调递增、不复用）；`utterance_id`（门禁每评估一段疑似语音分配一个）；`silence_epoch` / `text_version`（打分请求带上，返回时任一变化即作废，记 `stale_event_dropped(gate=endpoint)`）。事件里的 `turn_id` / `generation_id` 取数据自身携带的值，所以迟到事件在日志里仍归属旧 generation。

**事件**：需求要求的 15 类全部覆盖，另加 `script_marker`、`turn_score`（命中规则、p、置信度）、`speculation_*`、`bargein_decision`、`playback_resumed/underrun`、`context_committed`、`queue_overflow`、`provider_timeout`、`task_failed`、`generation_failed`、`cancel_grace_exceeded`、`close_timeout`。每条被拦下的数据都写 `stale_event_dropped`，payload 带 `gate`（`1/2/3/purge/finished/session_closed/endpoint`）和 `phase`（`late/rejected_on_wake/purge`）。

## 3. 并发与取消模型

```
Session.run() ── TaskGroup：session/audio_pump、session/asr_consumer、session/control、session/player、
                 session/gate-decide、gen-<id>/supervisor × N
                    └─ TaskGroup(generation)：gen-<id>/llm_bridge、gen-<id>/tts_bridge、gen-<id>/grace
```

- **invalidate（作废）**：同一个同步段内完成"切换 active id + 写 `generation_cancelled` + 同步 purge playback 队列 + 关闭该 generation 的 text 队列"，然后对 LLM/TTS 流调 `cancel()`（只是信号）。桥接任务继续排空上游，迟到项过闸被拦下记 `phase=late`，直到流结束或 `cancel_grace_ms` 到期（到期则取消桥接任务，记 `cancel_grace_exceeded`）。**新一轮不等排空，立即开始。**
- **finish（终止上游）**：用于 Provider 中途超时。generation 仍有效，已入队的音频照常播完，EOS 标 `truncated`。
- **三道闸**：闸① LLM→text、闸② TTS→playback、闸③ playback→AudioSink，校验 `generation_id == active`、未 `upstream_closed`、session 未关闭。`BoundedQueue.put()` 在等到空位后**同一同步步骤内重新校验**，阻塞中被作废的生产者醒来也写不进去（记 `rejected_on_wake`）。
- **幂等**：同一 generation 第二次 invalidate 只记一条 `duplicate=true`，不做任何事。
- **异常**：generation 内任务异常由 supervisor 用 `except*` 捕获，转成 `task_failed` + `generation_failed` 并作废该 generation，session 继续；session 级异常导致关闭（`reason=session_task_failed`）。任何地方都不吞 `CancelledError`。
- **关闭**：任何地方要关闭都只设 `stop_event(reason)`，由 `Session.run()` 执行：CLOSING → 作废所有 generation → 播放器 `closed` → 取消 session 循环 → 等 TaskGroup 退出（超过 `close_grace_ms` 继续等，每轮写 `close_timeout` 并再 `cancel()` 一次）→ 丢弃并记录队列残留 → `session_closed`。外部直接取消 `run()` 时关闭序列在 `finally` 中用 `shield` 跑完。测试断言关闭后没有本 session 的任务在运行。

## 4. Playback Truth

- **唯一事实来源**：播放器经过闸③写入 AudioSink 的帧。`playback_progress` 每 100ms 记录这段时间写入的帧区间 `[gen, 首帧, 末帧]`，停止时补记剩余区间，每一帧写入都有据可查。
- chunk 状态：`generated → synthesized → enqueued → played`，终态 `dropped(reason, gate)`。每个 chunk 关联文本范围（字符偏移）、样本数、裁剪后的词时间戳、是否入队、实际播放样本数、是否截断。
- generation 结束时 `playback_stopped.payload.playback_truth` 给出完整生成文本、入队文本、听到的文本范围与内容、实际播放时长。
- **词中截断**：只有结束时间 ≤ 已播位置的词算听到，播了一半的词算没听到（保守）。
- 上下文（`deque(maxlen=20)`）只写入听到的部分，被打断的标注 `(interrupted)`；一个字都没播出时只写用户文本。

## 5. Endpointing 策略

- **VAD 边界**：连续 3 帧语音 = `speech_start`；连续 4 帧静音 = `speech_end`，时间戳回溯到第一帧静音。
- **L1 打分时机**：`speech_end` 时打一次，之后文本（partial / final）每变化一次重打。
- **动态阈值** `high(t)`：静音 t < 200ms 不判停；200ms 时 0.85，线性降到 1000ms 时 0.5，此后不变；t ≥ 1500ms 无条件判停（兜底）。每 20ms tick 比较"当前分数 ≥ high(t) 且 confidence ≥ 0.5"。判停前最后一段的 final 没到时最多再等 300ms。
- **提前升级**：某级结果"拿不准"（0.3 < p < 0.85，或 confidence < 0.5，或超预算）就立即并行发起下一级；"很确定没说完"（p ≤ 0.3 且 confidence ≥ 0.5）不升级。当前分数取已返回的最高一级。预算 L1/L2/L3 = 50/150/500ms。`sample-output` 只配 L1；L2/L3 在单元测试中用按脚本查表的假打分器验证。
- **RuleTurnScorer（L1）词表与权重**，优先级从上到下：

| 规则 | 词表 / 条件 | (p, confidence) |
|---|---|---|
| `trailing_function_word`（强） | 句末是 for to of in on at with by from into about / and or but if because which / the a an my your our their / um uh er hmm；**压过句末标点** | (0.1, 0.9) |
| `trailing_function_word`（歧义） | 句末是 so that this these those her his its / is are was were be am will would can could do does did has have had，且无句末标点 | (0.4, 0.4) |
| `terminal_punct` | 以 `. ? !` 结尾（省略号不算） | (0.95, 0.8) |
| `trailing_content_word` | 其他 | (0.7, 0.55) |
| `few_words` | 词数 ≤ 2 时追加，置信度封顶 0.3 | — |

- **投机预生成**：静音中 L1 p ≥ 0.6 就启动（规范化文本变了才作废重启），音频进 playback 队列但门控 `held` 不出声，上限就是队列容量 75 帧。判停文本与预生成文本规范化后一致就放行，否则作废重来。A（预生成）turn-end→first-audio 460ms，A'（关闭）540ms。

## 6. 打断门禁与队列

- **门禁**：PLAYING 时 VAD 连续 3 帧语音 → 暂停（`paused_pending_decision`）。判定顺序：partial 出现完整指令词（stop / wait / no / hold on）→ 打断；final 为空或 VAD 结束后 400ms 仍无 final → 噪声；非空 final 只由附和词组成（uh-huh / mm-hmm / yeah / yep / right / okay / sure / I see / got it）且附和评分 ≥ 0.5（80ms 切片取最大，评分器超时 / 失败按搭腔）→ 搭腔；其余默认打断（final 到达，或 partial 已有 ≥ 2 个词且不可能是附和词组的前缀）。噪声 / 搭腔从暂停样本位继续播放；判为噪声后又来了同一段的词，会重新判定（`late_interrupt`）。
- **四种队列情况**：TTS 快于播放器 → playback 队列满时**阻塞 TTS**（背压，见 Q5）；ASR 慢于输入 → `asr_in` 满时**丢最旧帧**并记 `queue_overflow`（麦克风是实时的，不能阻塞）；Provider 长时间无返回 → 首包超时（LLM 2000 / TTS 1000，TTS 从第一段文本送入时起算）作废，chunk 间隔超时 1000ms 走 finish，阻塞在下游 put 的时间不计入；关闭时残留 → 丢弃并按队列记录在 `session_closed.residual`。

## 7. 场景补充说明

**B（犹豫停顿）**：信号是 VAD 静音时长、partial/final 文本、句子完整度（L1 规则）和动态阈值。"Please book it for" 命中强功能词，p=0.1、置信度 0.9（B trace `#37`），700ms 停顿结束时门槛约 0.63，0.1 永远够不着 0.5 的下限，所以停顿期间没有 `endpoint_committed` 也没有 `playback_started`；后半句 final 带句号 p=0.95，静音 200ms 时判停（endpoint latency 200ms）。取舍上 `initial_silence_ms=200` 决定最快多快判停，`high(t)` 的下降曲线决定拿不准时愿意多等多久；规则宁可给"拿不准"也不轻易判"说完"，偏向不抢话，预生成补回一部分延迟（A 460 vs A' 540）。trace 中的副作用：`speech_end` 时 partial 还停在 "Please book it"（p=0.7），启动了一个预生成，后来的低分不会作废它，它 held 着 32 帧直到 1420ms 才被替换并 purge（从未出声，只是浪费了算力，见 §8）。

**C（打断）**：开口 2400 → `speech_start` 2440（speech-start detection 40ms，3 帧确认，帧时间戳记在帧起点）→ 同一 tick 暂停（playback stop 40ms，目标 ≤ 250ms）→ partial "Stop" 2620 → `bargein_decision(command_word)` 2640（interruption decision 240ms）。overlap 40ms，即开口到暂停之间双方同时出声。判定后作废 gen 2、purge 65 帧，旧一轮上下文只写听到的 "Sure, I can help with that. Your table for two is booked for"，新一轮 endpoint latency 200ms、turn-end→first-audio 460ms。逐条分析见 [sample-output/analysis.md](sample-output/analysis.md)。

**D（80ms 噪声）**：靠"最小连续语音时长 + 是否识别出词"区分：3 帧才暂停，暂停后要识别出词才会打断；噪声的 final 是空文本，判为噪声并恢复。代价有两部分：真实打断的停播只多了 3 帧确认（实测 C 中 40ms），正式判定要等识别出词（C 中 240ms，但这时声音早已停了）；每次噪声会造成一段静音空隙，D 中噪声 2200 开始、2240 暂停、空 final 2460 到达、2480 恢复，`pause_total_ms=240`（剩余噪声 40ms + 噪声结束后等 final 约 180ms + 1 tick）。这正是"先暂停、确认后再取消"的方案：`false_interruption_count=0`，`false_pause_count=1`，回复最终播完。

**E（cancel 后迟到 chunk）**：不假设 `cancel()` 后上游立刻停：作废时先同步 purge 和切换身份，再发 cancel，桥接任务继续排空上游并逐个过闸。假 TTS 在 cancel（2720）后 +50ms 吐出 chunk 11，被闸②拦下（`phase=late`）；+3000ms 的 chunk 12 晚于新回复首帧（3740），且到达时 session 已在关闭（5380 请求关闭），被记为 `gate=session_closed`。`stale_chunk_received=2`、`stale_chunk_played=0`，排空完成后才写 `session_closed`（5720，`overdue_task_count=0`）。E 调大了 `cancel_grace_ms=4000`、`close_grace_ms=5000`，并**关闭了预生成**：假 TTS 对每个被取消的流都会吐迟到 chunk，被替换的预生成也会贡献迟到项，E 只想隔离"被打断的那个 generation"（所以 E 的 turn-end→first-audio 是 540 而不是 460）。

## 8. 取舍与已知局限

- **TTS 开头底噪裁剪**：能量阈值法，20ms 窗口 RMS < 1000 视为底噪，只裁每个 generation 的第一个 chunk，上限 `max_trim_ms=300`，词时间戳整体前移。它改善的是"首个可听到的词"的时间（测试：120ms 底噪 → 裁 1920 样本，可听起点提前约 120ms）；`turn_end_to_first_audio` 度量的是首帧写入（无论是不是底噪），所以这个指标不受影响。`sample-output` 的场景都没有配置底噪，`trimmed_samples=0`。
- **真实模型适配器只有接口**：Silero VAD、FireRedChat、Smart Turn、Jev 能实例化、符合同一 Protocol，但 I/O 形状、张量名、接口字段都是**未验证的占位**；推理是同步调用，不能放进虚拟 loop，真实时钟也未接入。
- **低分不撤销在途预生成**（见 B）：被替换或判停时才作废，浪费 LLM/TTS 算力但不出声；预生成失败后没有退避。
- **假 ASR 按能量切段**：相邻的语音段和噪声段之间没有静音就会被合成一段；`normalize_text` 会去掉撇号（don't → dont），"uh huh"（带空格）匹配不到 "uh-huh"。
- **路由边界**：PLAYING 期间结束的语音段，其迟到 final 可能拼到下一轮；回复播完后才到的迟到词不会进判停；`_end_turn` 时用户正说话会合成一个新轮次的开头，但没有写事件。
- **真实 Provider 下的边界**：看门狗在 chunk 中途杀掉 tts_bridge 时，账本里该 chunk 停在 `synthesized`；LLM 的 EOS put 没有 grace 上限。
- **指标**：暂停与开口的配对限定在本段结束前，极短的语音段可能漏配一次暂停。
- **回声（只写方案，未实现）**：真实场景中机器人自己的声音会被麦克风收进来，误触发打断。方案：播放器知道此刻正在播放哪段文字（Playback Truth 的词时间戳），ASR 识别出的词如果和"正在播放的文字窗口"吻合，就当作回声，不计入用户词数、不触发打断。假数据要用脚本把机器人声音真正混入麦克风输入来模拟，不能直接给帧打"回声"标签。

## 9. 设计问答

1. **谁是"实际播放状态"的唯一事实来源？** 播放器经过闸③写入 AudioSink 的帧，以 `playback_progress` 帧区间记入日志。chunk 状态、听到的文本、上下文、`stale_chunk_played` 指标都由它推出，不看 TTS 产出了什么。
2. **为什么 TTS cancel 不足以完成一次可靠打断？** cancel 只是信号：上游可能还在吐数据（E 中 cancel 后 +50ms、+3000ms 各来一个）；已合成的音频已经在 playback 队列里（C 中 65 帧）；LLM 还在给 TTS 喂文本；阻塞在 put 上的生产者醒来还会写。所以需要：先暂停播放器保证立刻没声音，再切换 generation 身份、同步 purge、关闭 text 队列，然后发 cancel，并由三道闸拦下所有迟到项。
3. **为什么 generated text 不能直接进下一轮上下文？** 生成 ≠ 听到。C 中 gen 2 生成到 "...a note about the quiet"，用户只听到 "...booked for"。如果把整段放进上下文，LLM 会以为用户已经听到"周四晚上七点"和备注等信息，下一轮回复会建立在用户没听到的内容上。
4. **固定静音阈值的问题？确认时间怎样影响 barge-in latency？** 阈值短会在犹豫停顿时抢话（B 的 700ms），阈值长则每一轮都要等满，响应慢；它不看用户说了什么。本实现用文本完整度 + 随静音下降的阈值：完整句 200ms 判停，"for" 结尾永远不判。打断这边，每多一帧确认停播就晚 20ms；本实现只用 3 帧确认停声（实测 40ms），把"等识别出词"的确认放在暂停之后（判定 240ms），所以确认时间主要影响判定延迟和噪声造成的空隙（D 中 240ms），不影响停声延迟。
5. **TTS 快于播放：阻塞、丢弃、限流还是取消？** 阻塞（背压）。playback 队列 75 帧（1.5s），满了 TTS 的 put 就阻塞，TTS 再反压 LLM。丢弃会让音频和 Playback Truth 出现空洞；取消只用于作废；限流在这里就是背压的效果。D 中队列打到上限 75，C 中 65。打断时队列的内容同步 purge，所以积压不会拖慢停播。
6. **拆成三个独立服务后，事件顺序和取消语义会多出哪些问题？** 没有了全局的 `sequence_number` 和统一时钟，跨服务事件只能靠 generation_id 加各自的单调序号排序，时钟偏差会让延迟指标失真；cancel 变成可能丢失、重复、乱序的消息，每一跳都要按 generation_id 做闸（fencing）并保证幂等；网络缓冲里在途的数据更多，迟到项变多、持续更久；背压要跨网络做（基于额度的流控）；Playback Truth 必须由最靠近扬声器的一端回报，有延迟；关闭时对端的孤儿流需要租约 / 超时回收。
7. **当前实现最可能在生产出的三个故障？** ① 回声：没有 AEC 和回声识别，机器人自己的声音会触发暂停甚至打断。② 真实 ASR 的时序远比假实现差：partial 延迟和抖动更大、final 可能迟到或分段与 VAD 不一致，按时间重叠归属 utterance 会出错，400ms 的 `noise_timeout` 可能把慢 ASR 下的真实说话先判成噪声（迟到词路径只能部分补救）。③ 阻塞调用卡住 event loop：适配器的推理是同步的，接到实时 loop 里会拖慢 20ms 的控制 tick 和播放 tick，停播延迟和播放连续性随之变差。

## 10. 最终默认参数（`RuntimeConfig`）

| 参数 | 值 | 参数 | 值 |
|---|---|---|---|
| VAD 开始 / 结束 | 3 帧 / 4 帧 | `initial_silence_ms` / `final_wait_ms` / `max_silence_ms` | 200 / 300 / 1500 |
| L1 / L2 / L3 预算 | 50 / 150 / 500 | `high_start` / `low` / `min_conf` | 0.85 / 0.3 / 0.5 |
| `high_floor` / `threshold_floor_at_ms` | 0.5 / 1000 | `speculate_threshold` | 0.6（默认开启） |
| 暂停所需语音帧 / `noise_timeout_ms` | 3 / 400 | 附和评分预算 / 阈值 | 100ms / 0.5 |
| LLM / TTS 首包超时 | 2000 / 1000 | chunk 间隔超时 | 1000 |
| `cancel_grace_ms` / `close_grace_ms` | 1000 / 2000（E：4000 / 5000） | 队列 asr_in / text / playback | 50 帧 / 64 段 / 75 帧 |
| 上下文 | 20 条 | `playback_progress` 间隔 | 100 |
| `max_trim_ms` / 裁剪阈值 RMS | 300 / 1000 | L2 音频环 / 附和音频上限 | 3000 / 2000 ms |

假 Provider：LLM 首包 / 间隔 300 / 50ms，TTS 200 / 40ms（每 chunk 约 200ms 音频），ASR partial / final 延迟 100 / 150ms，抖动上限各 20ms，各用独立 `Random(seed)`。`noise_timeout_ms=400 > 150 + 20 + 排队延迟` 有测试断言。
