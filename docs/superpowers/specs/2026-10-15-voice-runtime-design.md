# 最小 Voice Runtime —— 设计文档

> 日期：2026-10-15（v2：根据完整评审重写）
> 状态：待评审
> 需求来源：[docs/requirements.md](../../requirements.md)（下文用 `R-xxx` 引用需求编号；附录 A 是面试官原文，有出入以原文为准）
> 标注约定：**【用户决定】** = 讨论中由用户明确拍板；**【我的决定】** = 设计者给出的默认值，用户未反对，可以替换；未标注的为需求原文直接要求。

---

## 1. 意图与动机

这是一个面试展示用的 demo。面试官的评分全部集中在 Runtime 本身：状态、时序、并发、取消、可观测性。所以这个项目要证明的是：

> 即使上游 Provider 有延迟、有抖动、取消后还在吐数据，判停又要经过好几级不确定的打分，Runtime 依然能按时判停、按时停播，绝不播放过期音频，并且只看日志就能解释每一个决定。

真实模型（Silero、FireRed 等）在这个项目里只是"可以替换的打分器"：只接接口，核心测试全部跑确定性的假实现。【用户决定】

## 2. 目标 / 非目标 / 反目标

### 目标
- 场景 A～E 全部有自动化测试（§12），断言的是行为。
- 完整实现以下几块：分级判停（L1 用规则打分器）、投机预生成、打断门禁（搭腔判定 + 指令词白名单）、TTS 开头静音裁剪。
- 每个场景跑完都输出完整指标（§11.4），`sample-output/` 中附一次完整 barge-in 的 trace。
- 真实模型以可选适配器的形式接入：Silero VAD、FireRedChat-turn-detector、Smart Turn v3 / LiveKit v1-mini、Jev / LLM。【用户决定】

### 非目标
- UI、WebRTC、SIP、部署、多会话。
- 中文：只支持英文，与测试语句一致。【用户决定】
- 回声识别：只写方案，不实现。【用户决定】
- 兜底话术、工具调用 / 副作用。【我的决定】

### 反目标（技术上跑通了，但出现就算失败）
- 任何一个无界结构，包括队列、事件日志缓冲、上下文历史。
- 任何一帧过期音频被写入播放器（`stale_chunk_played > 0`）。
- 靠一个很大的固定静音阈值来通过场景 B。
- 测试依赖真实时间，或者同一个脚本两次运行得到的日志不一致。
- 下一轮上下文把"已生成"当作"已听到"。
- 只看日志解释不了某个决定。

## 3. 约束

- Python 3.12 + asyncio。【用户决定】结构化并发使用 `asyncio.TaskGroup`，层级见 §10.4。用 uv 管理依赖，pytest 跑测试。【我的决定】真实模型的依赖放在 optional extras 中。
- 音频格式：单声道 / PCM16 / 16kHz / 20ms 一帧（320 samples），每帧带单调递增的 `seq` 和时间戳。
- **只用虚拟时钟**【用户同意】：P0、P1 阶段，包括全部测试和 sample-output 的生成，都只用虚拟时钟。理由是 700ms 停顿、250ms 停播这类断言，只有在虚拟时钟下才能每次结果一致，测试不用真的等待，sample-output 也能逐字节复现。
  - **实现方式定死在 event loop 这一层**：自定义 event loop，重写 `time()`；没有就绪的回调时，直接跳到下一个定时器的时刻。这样 `asyncio.sleep`、`asyncio.timeout`、`wait_for` 全都自动走虚拟时间，不会有代码绕过时钟、偷偷用上真实时间。所有时间戳统一取 `loop.time()`（换算成毫秒）。
  - **核心代码禁止使用线程和真实 I/O 等待**（`to_thread`、executor、socket 等）。原因有两个：线程等待期间，虚拟时间会直接跳走；线程也没法被取消。事件日志是同步写文件，不受影响。
  - 真实时钟留到 P2 接入真实模型时再加，现在不做。
- 每个假 Provider 使用**各自独立的 `random.Random(seed)`**，避免调用顺序变化导致抖动值变化。
- 遵守 [CLAUDE.md](../../../CLAUDE.md)：不做没要求的功能，不做只用一次的抽象。

## 4. 架构总览

```
 ScriptedAudioSource ──帧──► Session（唯一 owner：ID、状态机、TaskGroup、active_generation_id）
   │ 另写 script_marker                │
   │ （评测真值，Runtime 不读）         ├─► VAD（逐帧同步） ─► SpeechTracker（speech_start/end，silence_epoch）
   │                                   ├─► [asr_in 有界队列] ─► ASR（session 级流）─► partial/final
   │                                   │
   │                                   ├─► EndpointCascade：L1 规则 ─► L2 ─► L3 ─► 硬上限
   │                                   │        └─► SpeculationManager（同一时间至多 1 个）
   │                                   ├─► BargeInGate（播放期间：暂停 / 判定 / 恢复 / 打断）
   │                                   │
   │                                   └─► GenerationSupervisor{gen_id}
   │                                          LLM ─闸①─► [text 队列] ─► TTS ─裁剪─闸②─► [playback 队列(帧)]
   │                                                                                   │
   │                                                              Player（门控状态 + 闸③）─► AudioSink
   │                                                              播放进度 = Playback Truth 唯一事实来源
   └──────────────────────────── 所有组件 ──同步写──► EventLog(JSONL 文件) ──离线──► Metrics
```

**谁来指挥**【我的决定】：Session 是中心协调者。各阶段之间用有界队列流式衔接，但以下动作只有 Session 能做：启动 / 作废 / 终止 generation、切换 `active_generation_id`、改变播放器的门控状态、关闭 session。

## 5. Provider 接口

| 接口 | 形态 | 取消语义 | 假实现的可配置项 |
|---|---|---|---|
| `VAD.process(frame) -> float` | 逐帧同步调用，返回语音概率 | 无 | 由脚本段落类型决定概率（语音 / 噪声给高分，静音给低分） |
| `ASR.run(frames: AsyncIterator[Frame]) -> AsyncIterator[AsrEvent]` | session 级长流；`AsrEvent{kind: partial\|final, text, words[{w, start_ms, end_ms}], segment_id, segment_start_ms, segment_end_ms}` | 随 session 关闭 | 假 ASR 按脚本的段落切分：每个用户语音段、每个噪声段各产生一个 segment。每个词说完后经过 `partial_latency_ms` 吐出 partial；段落结束后经过 `final_latency_ms` 吐出 final。**噪声段的 final 文本为空**。支持抖动和卡住 |
| `LLM.start(messages) -> ProviderStream[str]` | 异步迭代器 + `cancel()` | `cancel()` **只是发一个信号**，之后流仍可能吐出迟到项，最终结束 | 首包延迟、chunk 间隔、抖动、在第 N 个之后卡住（等待一个可被取消的 Event）、在第 N 个时抛异常、取消后仍吐 N 个迟到项（**每个迟到项相对 cancel 的到达时刻**都可以配置，且必须 < `cancel_grace_ms`） |
| `TTS.start(text_stream: AsyncIterator[str]) -> ProviderStream[AudioChunk]` | 双流式；`AudioChunk{samples, text_range, word_timings}`。`text_stream` 结束（EOS）后，TTS 把剩余音频吐完就结束 | 同 LLM | 同 LLM，另有 `leading_noise_ms`（开头底噪） |
| `TurnScorer.score(text, audio?) -> TurnScore(p_complete, confidence)` | async | 超时由调用方负责 | L1 = 规则打分器（§6.3）；L2 / L3 = 按脚本查表，延迟、超时、异常都可配置 |
| `BackchannelScorer.score(slices: list[80ms 音频]) -> list[float]` | async | 超时由调用方负责（预算见 §16） | 按 utterance 的脚本给每片打分；延迟、超时、失败都可配置 |

**超时由谁负责、怎么计时**【我的决定】：由消费者（GenerationSupervisor 里的桥接任务）计时，只统计"等待上游下一项"的时间，**阻塞在下游 put 上的时间不计入**。TTS 首包超时从第一段文本送进 TTS 的时刻开始算，所以 LLM 慢不会被错算成 TTS 超时。打分器超时等于本级预算（§6.2）。打分器抛异常按超时处理，不算 session 级故障。默认值见 §16。

## 6. 分级判停（Endpointing Cascade）

### 6.1 各级
| 级别 | 默认（测试） | 可选真实实现 | 输入 |
|---|---|---|---|
| L0 声学 | FakeVAD | Silero VAD（20ms 帧需要拼成 512 samples 的块） | 帧 |
| L1 快速语义 | **RuleTurnScorer**（§6.3）【用户决定】 | FireRedChat-turn-detector（ONNX） | 当前累计文本 |
| L2 声学+语义复核 | FakeTurnScorer | Smart Turn v3 或 LiveKit v1-mini | 最近一段音频 + 文本 |
| L3 兜底 | FakeTurnScorer | Jev（TypeSafe AI 的闭源判别式决策 API，返回带置信度的类型化结果，厂商自报 70～500ms），也可以换成通用 LLM | 文本 |
| 保底 | — | — | 静音时长硬上限 |

【用户决定】按级打分，拿不准就升级到更好的模型。L2 不用 TEN 7B，改用"能听语调的小模型"，这是【我的决定】。

### 6.2 决策流程【我的决定，参数可配】
- **VAD 语音段边界**：连续 3 帧语音判为 `speech_start`；连续 4 帧（80ms）静音判为 `speech_end`。`speech_end` 的时间戳回溯到第一帧静音的时刻，静音时长从这个时刻开始计算。
- **silence_epoch 和 text_version**：每次 `speech_start`，epoch 加 1；累计文本每变化一次，text_version 加 1。所有打分请求都带上这两个值，结果返回时只要其中一个变了，就记 `stale_event_dropped(gate=endpoint)`。比如 final 到达改变了文本，正在进行的 L2/L3 结果就会作废。
- **L1 打分时机**：在 `speech_end` 那一刻，先用当前文本（通常是 partial）打一次分。之后只要文本变化（新的 partial 或 final）就重新打分，文本不变就不重复打分。L1 的分数有两个用途：
  1. 决定是否预生成（§7）：在静音期间，只要 p ≥ `speculate_threshold`（0.6）就可以启动。
  2. 判停：见下面的"动态阈值"。
- **提前升级**【用户同意】：某一级给出的结果是"拿不准"（`low < p < 0.85`，或者 `confidence < min_conf`，或者超出了本级预算），就**立即**并行发起下一级，不用等静音满 `initial_silence_ms`。结果先存起来，到可以判定的时候再用。L2 的 150ms 基本被静音期吸收掉。
  - 在同一个 (epoch, text_version) 下，每一级同时**最多只有一个请求在途**。
  - 某一级的结果是"很确定没说完"（`p ≤ low` 且 `confidence ≥ min_conf`），就不再往上升级。
  - L3 是最后一级，不再升级。
- **当前分数**：在当前 (epoch, text_version) 下，**已经返回的最高一级**的结果。
- **动态阈值判停**【用户同意】：静音时长为 t 时，判停门槛 `high(t)` 的取值为：
  - t < `initial_silence_ms`（200ms）：不判停
  - 200ms 时为 0.85，之后线性下降，到 `threshold_floor_at_ms`（1000ms）时降到 `high_floor`（0.5），此后保持不变
  - t ≥ `max_silence_ms`（1500ms）：无条件判停（`reason=max_silence`），这只是兜底，不是主信号

  每个 20ms tick 都用"当前分数"和 `high(t)` 比较一次（只是比较，不重新打分）。`p ≥ high(t)` 且 `confidence ≥ min_conf` 就判停（`reason=score_threshold`）。
- **为什么这样设计**：
  - "很确定没说完"（p ≤ 0.3）永远达不到 0.5 的下限，所以一定会等到用户再开口或者硬上限。场景 B 中，700ms 时门槛约为 0.63，"for" 结尾的低分（约 0.1）离它还很远。
  - 拿不准的中间分会随着静音变长逐渐被接受。比如 p = 0.6 时，大约在 770ms 判停，不必等满 1500ms。P0 阶段还没有 L2/L3 时，也按这个规则走。
- **每级预算**：L1 50ms、L2 150ms、L3 500ms。
- **判停文本**：如果判停时最后一个语音段的 final 还没到，最多再等 `final_wait_ms`。等到了就用 final；超时就用最新的 partial，并记 `text_source=partial`。
- **`endpoint_committed` 的写入时刻**：判停文本确定之后才写。payload 中带 `decided_at`、`text_source`、`reason`、`level`、`p`，以及判停时刻的 `threshold`（即 `high(t)` 的值）。只看日志就能知道"为什么在这一刻判停"。

**L1 规则冲突的优先级**：句末是介词、连词等"没说完"信号的规则，优先于句末标点规则。例如 "Please book it for." 仍然判为"没说完"。

**场景 B 的成立方式**：L1 规则识别出 "Please book it for" 以介词结尾，给出低分，所以在 700ms 停顿期间不会判停，也不会预生成。后半句说完后，文本以时间词结尾并带有句末标点，L1 给出高分，静音约 200ms 时就判停。整个过程没有用到很大的固定阈值。

**"响应快"和"不抢话"之间的取舍**：`initial_silence_ms` 决定了说完后最快多久能判停；`high(t)` 的下降曲线决定了拿不准时愿意多等多久。规则打分器宁可给出"拿不准"，交给更高一级去判断，也不轻易判成"说完了"，这是偏向"不抢话"的取舍。预生成在一定程度上弥补了因此多等的时间。

### 6.3 L1 默认实现：确定性规则打分器【用户决定：方案二】
L1 的分数来自文本本身的语言信号，不按脚本查表。规则方向如下（具体词表和权重由实现者决定，并写进 DESIGN.md）：
- 句末是介词、连词、冠词、助动词或填充词（`for / to / and / the / a / is / um` 等）→ 强烈倾向"没说完"，置信度高
- final 带句末标点 → 倾向"说完了"
- 以时间词、名词短语等可以结尾的成分收尾 → 倾向"说完了"
- 词数很少 → 降低置信度，因此更容易升级到下一级

`turn_score` 事件的 payload 中列出**命中了哪些规则**。这套规则需要有单元测试。

## 7. 投机预生成

【用户决定】使用级联方案：流式 ASR、流式 LLM、双流式 TTS。预生成同时产出文字和音频，但先不播放；用户说完后再确认，发现改口就推倒重来。
- **触发**：在静音期间，L1 对当前文本打分 p ≥ 0.6 时启动。【用户同意】只有当前文本（规范化之后）发生变化时，才会作废旧的预生成并重启一个新的，以免频繁抖动。
- **数量**：同一时间最多一个预生成。
- **text 队列每个 generation 各有一个**，由该 generation 的 supervisor 持有，不在 generation 之间共享。
- **存放位置与上限**：只有一处，就是 playback 队列。播放器处于 `held` 门控状态时，队列里的音频不会被播出。所以上限就等于 playback 队列的容量（约 1.5 秒音频），队列满了 TTS 就被阻塞。【用户决定：上限与队列挂钩】
- **确认**：判停时，比较"预生成所依据的文本"和"判停文本"，两者规范化后（小写、去掉标点、合并空白）完全一致就采用：门控从 `held` 切到 `playing`，generation 继续往下生成。不一致就作废（§10.1 invalidate），用判停文本重新生成。【我的决定】
- 判停时如果没有进行中的预生成，就直接用判停文本启动一次 generation，再立即放行。
- 预生成不得执行任何副作用；被作废的预生成，其文字不进上下文。

### 7.1 延迟优化：裁掉 TTS 开头的静音底噪【用户决定】
- **在哪裁**：TTS 产出之后、闸②之前，只裁每次 generation 音频的开头。
- **怎么裁**：留到后续测试时确定（候选方法：能量阈值、按 Provider 固定偏移、用 VAD 判断）。约束是：不能裁掉真实语音，单次裁剪量要有上限。
- **时间戳**：裁掉多长，词时间戳就整体往前平移多长。Playback Truth 使用裁剪后的时间轴。
- `tts_chunk` 事件的 payload 中记录 `trimmed_samples`。

## 8. 身份、状态与播放器门控

### 8.1 ID 体系【我的决定】
| ID | 何时创建 | 谁持有 |
|---|---|---|
| `session_id` | Session 构造时 | Session |
| `turn_id` | 以下两种情况各递增一次：① 在 LISTENING 状态下、没有进行中的轮次时，出现第一个 `speech_start`；② 门禁确认为打断时（发起打断的这段话成为新一轮）。播放期间疑似说话的语音不递增 turn_id | Session |
| `utterance_id` | 播放期间，门禁每开始评估一段疑似用户语音时分配 | BargeInGate |
| `generation_id` | 每次启动 LLM→TTS（包括预生成）时递增；在整个 session 内单调递增，永不复用 | Session 分配，所有数据都携带它 |
| `silence_epoch` | 见 §6.2 | SpeechTracker |

**事件里的 `turn_id` 和 `generation_id` 取自数据本身携带的值**，不取 Session 的当前值。所以旧 generation 的迟到事件在日志里仍然归属于旧的 turn 和 generation。

**轮次什么时候结束**【我的决定】：出现以下任一情况，一轮就结束：
- 回复播完（completed）
- 截断后已入队部分播完（truncated）
- generation 失败（failed）
- 被用户打断（interrupted）

轮次结束时：
- 写 `context_committed`：用户这一轮的完整文本，以及助手实际听到的部分。
- 清空累计文本。

下一次在 LISTENING 状态下出现 `speech_start`，才开启新的一轮。在 pending 阶段用户继续说话时，轮次并没有结束，所以不提交上下文，文本继续累计。

### 8.2 Session 状态机
```
LISTENING ──判停──► RESPONDING(pending: 已判停，尚未出声)
    ▲                    │ 首帧播出                      用户开口(≥3帧) → 走同一套门禁（门控仍是 held，没有声音要停）：
    │                    ▼                               搭腔/噪声 → 保留 generation；打断 → 作废(reason=user_continued)，
    │                                                    回到 LISTENING，同一 turn 继续累计文本
    │              RESPONDING(playing) ◄──搭腔/噪声：从暂停样本位继续──┐
    │                    │ 疑似说话(≥3帧)                              │
    │                    ▼                                             │
    │              RESPONDING(paused) ──────────────────────────────────┘
    │                    │ 门禁判为打断：作废旧 generation，turn_id+1
    └────────────────────┘ （判停用这段话的 speech_end 时间戳补算静音）
   播放完 / 被截断播完 / generation 失败 ──► LISTENING（轮次结束，见 §8.1）
任意状态 ──close / 断开 / session 级异常──► CLOSING ──► CLOSED
```
预生成发生在 LISTENING 状态里，不改变状态。

### 8.3 播放器门控（统一的一套）【我的决定】
播放器只有一个门控状态：`idle / held / playing / paused / closed`。
- `held` 用于预生成还没放行时，`paused` 用于门禁暂停时，两者共用同一套机制。
- 播放器每 20ms tick 一次：只有处于 `playing` 时，才从 playback 队列取一帧，经过闸③校验后写入 AudioSink。所以门控一旦变化，最多 20ms 内声音就会停止。
- **EOS（流结束标记）**：LLM 流结束时，往该 generation 的 text 队列写 EOS；TTS 流结束时，往 playback 队列写一个带 `generation_id` 的 EOS。播放器读到 EOS，才写 `playback_stopped(completed|truncated)`，并结束这一轮。
- **欠载**：playback 队列已空但还没读到 EOS 时，不写 `playback_stopped`；欠载开始时写一条 `playback_underrun`。
- **`playback_stopped.reason` 的取值**：`paused_pending_decision`、`interrupted`、`completed`、`truncated`、`generation_failed`、`session_closed`。

### 8.4 TTS chunk 状态（Playback Truth）
`generated`（LLM 文本已产出）→ `synthesized`（TTS 音频已产出并裁剪过，还没进 playback 队列，比如正被背压阻塞）→ `enqueued`（已写入 playback 队列）→ `played`（播放器回报已播放）。另有终态 `dropped(reason, gate)`。

- `enqueued` 只表示"进了播放队列"，不代表一定会播：预生成被作废时，已入队的部分会转为 `dropped(reason=speculation_discarded)`。
- **playback 队列以 20ms 帧为单位**：TTS chunk 在入队前切成帧，每帧携带 `generation_id`、`chunk_id` 和帧序号。chunk 的状态和播放量由它的各帧汇总得出。

## 9. 打断门禁（Barge-in Gate）

【用户决定】区分真打断和噪声，看两样东西：持续时间，以及有没有识别出词。

### 9.1 流程
1. **暂停**：处于 RESPONDING(playing) 时，VAD 连续检测到 3 帧（60ms）语音，就把门控切到 `paused`，分配 `utterance_id`，记下暂停时的样本位置，并写 `playback_stopped(reason=paused_pending_decision)`。
2. **判定**（按顺序，先命中先生效）。只要 VAD 仍判定在说话，就保持暂停，不会恢复播放。ASR 的结果按时间重叠归到 utterance：`segment_start_ms`～`segment_end_ms` 与这段 utterance 的 VAD 区间有重叠，就算属于它。
   1. partial 中出现**完整的指令词**（`stop / wait / no / hold on`）→ **打断**。不必等这段话说完。
   2. **噪声**：这段话的 final 文本为空；或者 VAD 已判定说完，又过了 `noise_timeout_ms` 仍没有 final（防止 ASR 卡住）。`noise_timeout_ms` 必须大于 `final_latency_ms + 最大抖动 + asr_in 排队延迟`，测试中要断言这个不等式成立。（噪声放在搭腔之前判断，这样空文本永远不会被误判为搭腔。）
   3. 已收到这一段**非空**的 final，并同时满足以下三条 → **搭腔**：
      - 文字：整段话只由附和词组成（`uh-huh / mm-hmm / yeah / yep / right / okay / sure / I see / got it`）
      - 时机：机器人正在说话。门禁只在 playing、paused、pending 这三个状态下运行，所以在门禁内部这一条天然成立，保留它是为了与用户给出的规则一一对应
      - 声音：附和评分 ≥ 0.5。把这段话切成 80ms 小片，每片打分，**取最大值**。【用户决定】评分器超时或失败时，纯附和词**按搭腔处理**。【用户决定】
   4. **其余情况 → 默认判为打断**。【用户决定】判定时机有两种：收到这一段的 final 时；或者 partial 中已有的完整词序列**既不等于、也不是**任何附和词组的前缀，并且至少有 2 个词。所以 "I"、"I see" 都不会被提前判为打断。
3. **打断**：作废旧 generation（§10.1），清空 playback 队列，并写 `playback_stopped(reason=interrupted)`，payload 中附带 Playback Truth 摘要（§11.2）。turn_id 加 1，这段话成为新一轮的用户输入，累计文本从这段话的开头算起。然后进入 LISTENING，判停用这段话的 `speech_end` 时间戳开始计算静音。
4. **搭腔 / 噪声**：
   - 搭腔的转写直接丢弃：不作为用户输入提交，不进下一轮上下文。【用户决定】
   - 门控切回 `playing`，从暂停时的样本位置继续播放，不重复、不漏播，并写 `playback_resumed`。【用户决定】
   - 这次停顿时长记入指标。

### 9.2 "停播"的定义与三段延迟【我的决定】
"用户开口到停播 ≤ 250ms"中的"停播"，指播放器停止往外输出声音，也就是第一条 `playback_stopped`（`reason=paused_pending_decision`）出现的时刻。三段延迟都以 `script_marker` 记录的真实开口时刻为起点：
- **speech-start detection**：开口 → `speech_start`
- **playback stop**：开口 → 第一条 `playback_stopped`，目标 ≤ 250ms
- **interruption decision**：开口 → `bargein_decision(result=interrupt)`。这一段可以超过 250ms，因为声音早已停止。

**overlap duration** = min(用户这段话结束, 第一条 `playback_stopped`) − 开口时刻；如果开口时还没有声音在播（pending 阶段），overlap 为 0。

**这项保护的代价**（按默认参数估算，写进 DESIGN.md）：
- 停播只多等了 3 帧确认，约 60ms。
- 正式判定要等识别出词：指令词的判定延迟约等于这个词的时长加上 `partial_latency_ms`。
- 噪声会让播放出现一段空隙，长度约等于噪声时长 + 80ms 的 speech_end 确认 + `final_latency_ms`（默认值下约 300ms）。这就是"先暂停、确认后再取消"的代价。

### 9.3 迟到的词
判为噪声、恢复播放之后，又收到同一个 `utterance_id` 的 ASR 结果时，重新执行一遍 §9.1 第 2 步：
- 判为打断：立即暂停并打断，日志中标记 `late_interrupt=true`。
- 判为其他：丢弃。

### 9.4 回声（只写方案，不实现）
播放器知道"此刻正在播放哪段文字"。如果 ASR 识别出的词和正在播放的文字窗口吻合，就当作回声，不计入用户的词数。假数据必须用脚本把"机器人声音混入麦克风"模拟出来，不能直接给每帧标上"这是回声"。

## 10. Generation 隔离、取消与并发结构

### 10.1 两种结束方式【我的决定】
| 操作 | 含义 | 用于 |
|---|---|---|
| **invalidate**（作废） | 切换身份，旧数据全部视为过期 | 用户打断、预生成作废、pending 阶段用户继续说、首包超时、generation 级任务异常、session 关闭 |
| **finish**（终止上游） | 让 LLM 和 TTS 停下来，但 generation 仍然有效；已入队的音频照常播完，标记为被截断 | Provider 中途超时 |

**invalidate 的步骤**（第 1、2 步在同一个同步段内完成，中间没有 await）：
1. 设置 `active_generation_id = None`（或新的 id），把旧 generation 标为 cancelled，写 `generation_cancelled(reason)`。
2. 从 playback 队列中**同步清除**旧 generation 的帧，写一条汇总的 `stale_event_dropped(gate=purge, phase=purge, frames=N)`。同时**关闭旧 generation 自己的 text 队列**（写入 EOS），让旧 TTS 的输入流正常结束；并唤醒所有阻塞在 put 上的生产者。
3. 调用 `llm_stream.cancel()` 和 `tts_stream.cancel()`。这只是发一个信号，**不假设上游会立刻停下**。
4. 桥接任务**继续排空上游**：每收到一个迟到项都过闸并被拦下，写 `stale_event_dropped(phase=late)`，直到上游流结束，或者从 cancel 起算 `cancel_grace_ms` 到期。
5. 到期后上游还没结束，supervisor 就取消自己 TaskGroup 里的 bridge 任务，并写 `cancel_grace_exceeded`。

新一轮**不等**第 3～5 步完成，立即开始。【用户决定】

**例外**：如果 invalidate 是由 generation 内部的任务异常引起的，TaskGroup 已经自动取消了其他 bridge，第 4 步的排空就不会发生。这条路径只执行第 1、2 步，日志里通过 `task_failed` 体现。

**finish 的步骤**：
1. 把这个 generation 标为 `upstream_closed`。之后闸①、闸②遇到它的数据一律拒绝，记 `gate=finished`。
2. 调用两个流的 `cancel()`，并在 playback 队列里已有的帧之后写入 EOS（`truncated=true`）。
3. 排空上游的方式与 invalidate 的第 4、5 步相同。

播放器播完已入队的帧，读到 EOS 后写 `playback_stopped(truncated)`。

**stale 计数的三种口径**【我的决定】：`stale_event_dropped.payload.phase` 区分以下三种情况：
- `late`：cancel 之后才从上游收到的项，以 chunk 为单位。**只有这一种计入 `stale_chunk_received`**。
- `rejected_on_wake`：cancel 之前就已产出、阻塞在 put 上、醒来后被拒的项。
- `purge`：同步清除的帧。

后两种分别计入 `stale_rejected_on_wake` 和 `stale_purged_frames`。

### 10.2 三道闸【用户决定】
每道闸都校验三件事：`item.generation_id == active_generation_id`；对闸①和闸②来说，这个 generation 还不是 `upstream_closed`；session 未关闭。不通过就丢弃，并写 `stale_event_dropped`，payload 中**必须带 `gate`**，说明在哪一道被拦下。【用户决定】
- 闸①：LLM → text 队列
- 闸②：TTS（裁剪后）→ playback 队列
- 闸③：playback 队列 → AudioSink 写入（最后一道防线）
- 另有几种 gate 值：`purge`（invalidate 时同步清除）、`finished`（finish 之后上游的迟到项）、`session_closed`（关闭后的任何写入）、`endpoint`（迟到的打分）

**消除"先检查、后入队"的竞态**：所有队列都是自定义的 `BoundedQueue`。`put()` 等到有空位之后，在**同一个同步步骤里重新校验**闸的条件，校验通过才写入；不通过就记为闸①或闸②拦截。所以即使生产者在已满的队列上阻塞时 generation 被切换了，它醒来后也写不进去。旧数据因此不会占住容量，也就不会出现"新的预生成等容量、播放器等放行"这种死锁。

### 10.3 并发 cancel
invalidate 按 `generation_id` 做到幂等：同一个 generation 第二次被 invalidate 时，只写一条日志，不做任何事；目标不是当前 generation 时直接忽略。

### 10.4 任务层级【我的决定】
```
Session.run()  ── TaskGroup(session)
  ├─ audio_pump（音频源 → VAD → asr_in）
  ├─ asr_consumer
  ├─ cascade / gate 驱动
  ├─ player_ticker
  └─ GenerationSupervisor(gen_id) × N    ← 每个 generation 一个
        └─ TaskGroup(generation)：llm_bridge、tts_bridge
           supervisor 用 except* 捕获子任务的异常，转成 task_failed + generation_failed，
           然后 invalidate 这个 generation；异常不会继续抛到 session 的 TaskGroup。
           supervisor 同时负责 cancel_grace 看门狗。
```
- **所有任务都显式命名**，比如 `gen-7/tts_bridge`。日志中只写任务名和"异常类型: 消息"，不写自动生成的 `Task-N`，也不写带内存地址的 repr，以保证日志逐字节一致。
- **谁来执行关闭**：任何地方要关闭 session（断开脚本段、外部调用 `close()`）都只做一件事：设置 `stop_event(reason)`。session 的主循环等到这个事件后，由 `Session.run()` 按 §13 的顺序执行关闭。任何任务都不会 await 关闭流程本身，所以不会自锁。
- session 级任务的异常会冒泡到 session 的 TaskGroup，TaskGroup 会自动取消所有子任务。`Session.run()` 在 TaskGroup 外层用 `except*` 捕获，再执行 §13 的"异常路径"关闭序列。异常**不再向外抛出**，`run()` 正常返回关闭原因，异常摘要写进 `session_closed`。
- 所有任务都挂在 session 的 TaskGroup 之下，因此不会产生孤儿任务。

## 11. 有界结构、Playback Truth 与可观测性

### 11.1 队列（全部自定义 `BoundedQueue`，容量可配置，测试可以读取）【我的决定】
| 队列 | 单位 / 默认容量 | 满了怎么办 | 对应需求里的哪种情况 |
|---|---|---|---|
| `asr_in` | 帧 / 50（1s） | 丢弃最旧的一帧，写 `queue_overflow` 并计数 | ASR 慢于输入：麦克风是实时的，不能阻塞 |
| `text`（每个 generation 一个） | 文本片段 / 64 | 阻塞 LLM（背压） | — |
| `playback` | 20ms 帧 / 75（1.5s） | 阻塞 TTS（背压） | TTS 快于播放器：限流，不丢弃 |

- **Provider 长时间无返回**：由首包超时和 chunk 间隔超时处理（§5、§13）。
- **关闭时队列里还有残留**：丢弃，并按队列分别记录残留数量（§13）。
- 每个队列自己记录 `max_depth`。入队和出队相关的事件（`audio_enqueued`、`queue_overflow`）在 payload 中带上 `queue` 和 `depth`。所有队列的 `max_depth` 汇总写进 `session_closed`。
- **其他有界结构**：
  - 事件日志**同步追加写入 JSONL 文件**，不经过内存队列。
  - 上下文历史只保留最近 20 条消息。
  - L2 需要的"最近一段音频"使用环形缓冲，上限 3s。
  - 附和评分用的整段 utterance 音频上限 2s（超过 2s 的话不会是附和），超出部分不再缓存。

### 11.2 Playback Truth
- **唯一事实来源**：播放器写入 AudioSink 的帧。`playback_progress` 每 100ms 记录一次，内容是这 100ms 里写入的**帧区间**（`generation_id` 加上起止帧号）。每次停止播放时，再补记一次尚未记录的区间。这样每一帧写入在日志中都有据可查。
- 每个 TTS chunk 都能关联到：`generation_id`、文本范围（字符偏移）、样本数、裁剪后的逐词时间戳、状态、是否已入队、实际播放的样本数、是否被截断。
- **每次 generation 结束时**（播完、被打断、被截断、session 关闭），在 `playback_stopped` 的 payload 中附上 `playback_truth` 摘要：
  - 完整生成文本
  - 入队文本
  - 实际听到的文本范围和文本内容
  - 实际播放时长
- **切在词中间时的近似**【我的决定】：只有结束时间 ≤ 已播放位置的词才算"听到"，播了一半的词算没听到（保守估计）。
- **上下文写入**：轮次结束时（§8.1）写 `context_committed`，内容包括这一轮的用户文本，以及助手实际听到的部分（`interrupted` 标志）。被打断的助手回复标注 `(interrupted)`。助手一个字都没播出时，只写入用户文本。

### 11.3 事件日志
JSONL 格式，每条包含：`timestamp`（时钟毫秒）、`session_id`、`turn_id`、`generation_id`、`event_type`、`sequence_number`（在 session 内全局递增）、`payload`。

- **需求要求的事件**：`audio_frame`、`speech_start`、`speech_end`、`endpoint_committed`、`asr_partial`、`asr_final`、`llm_chunk`、`tts_chunk`、`audio_enqueued`、`playback_started`、`playback_progress`（每 100ms 一条）、`playback_stopped`（带 `reason`）、`generation_cancelled`、`stale_event_dropped`、`session_closed`
- **本设计新增的事件**：
  - `script_marker`：评测真值，只由脚本源写入，带 `eval_only=true`，Runtime 任何组件都不读取
  - `turn_score`：级别、p、confidence、命中的规则、耗时、决定、epoch
  - `speculation_started` / `speculation_adopted` / `speculation_discarded`
  - `playback_resumed`、`playback_underrun`
  - `bargein_decision`：结果、命中的规则、词、评分、`utterance_id`、`late_interrupt`
  - `context_committed`、`queue_overflow`、`provider_timeout`、`task_failed`、`generation_failed`、`cancel_grace_exceeded`、`close_timeout`
- `audio_frame` 默认每帧都记录。也可以配置成采样记录，任何指标都不依赖它，是否采样不影响指标结果。

### 11.4 每个场景的指标（只从日志离线计算）【我的决定】
| 指标 | 怎么从日志算出来 |
|---|---|
| endpoint latency | 用户真实说完（`script_marker`）→ `endpoint_committed` |
| turn-end → first-audio | `endpoint_committed` → 本次 generation 的 `playback_started` |
| 打断三段延迟 + overlap | 见 §9.2 |
| false interruption count | 被打断取消的 generation 中，对应语音段在 `script_marker` 里标为非用户语音的次数（即错误地取消了回复）|
| false pause count / 总停顿时长 | 暂停之后又 `playback_resumed` 的次数，以及这些停顿的总时长 |
| stale chunk received | `stale_event_dropped` 中 `phase=late` 的 TTS chunk 数 |
| stale chunk played | `playback_progress` 记录的帧区间中，`generation_id` 在写入之前（按 `sequence_number` 比较先后）已经出现 `generation_cancelled` 的帧数，应为 0 |
| stale purged frames / rejected on wake | 分别对应 `phase=purge` 和 `phase=rejected_on_wake`，单独展示，不计入上面两项 |
| 各队列最大长度 | `session_closed.payload.queue_max_depth` |

## 12. 测试策略
- **虚拟时钟**：采用 §3 规定的 event loop 级实现。必须保证**同一个脚本每次运行，生成的 JSONL 逐字节一致**，并有一个测试断言这一点。
- **脚本化音频源**：用"用户语音段（文本 + 逐词时间）/ 静音 / 噪声 / 断开"这样的时间轴描述场景，并写 `script_marker`。脚本跑完后**持续发送静音**，直到测试显式调用 `close()`，所以回复能完整播完。客户端断开要作为一个显式的脚本段来写。
- **测试的外层超时**：每个测试都包一层真实时间的超时，Provider 卡住时测试会失败，而不是一直挂住。
- **场景测试**（断言里的界值都由当前配置推算出来，并在测试中写明推算方法）：
  - **A'**（关闭预生成，P0）：对应需求 A 的字面顺序"先判停，再 ASR / LLM / TTS"。断言 turn-end → first-audio ≤ LLM 首包 + TTS 首包 + 各自的最大抖动 + 2 个 tick（40ms），证明除了 Provider 固有的延迟之外没有额外排队。
  - **A**（开启预生成，P1）：这是在 A' 基础上的优化。断言预生成被采用；`playback_started` 晚于 `endpoint_committed`；turn-end → first-audio < A' 的同一指标。
  - **B**：停顿期间既没有 `playback_started`，也没有 `endpoint_committed`；后半句说完后 endpoint latency ≤ `initial_silence_ms` + `final_wait_ms` + L1 预算。
  - **C**：开口到第一条 `playback_stopped` ≤ 250ms；之后出现 `generation_cancelled`，playback 队列被清空；**旧一轮**的 `context_committed` 中，助手文本只有听到的部分，并带 `interrupted=true`；**新一轮**的用户文本是 "Stop. Make it Friday instead."；新一轮的回复开始播放。
  - **D**：80ms 噪声后出现 `playback_resumed`，回复最终播完；`false_interruption_count == 0`，`false_pause_count == 1`。
  - **E**：假 TTS 在 cancel 之后再吐 2 个 chunk，第 1 个在 cancel 后约 50ms 到达，第 2 个**晚于新 generation 的首帧**到达。由于新一轮要先等用户说完、再判停、再生成，新首帧距离 cancel 通常超过 1s，所以 E 的测试配置需要调大 `cancel_grace_ms`，满足：**新首帧相对 cancel 的偏移 < 第 2 个迟到 chunk 的到达偏移 < `cancel_grace_ms`**。测试里要写出推算过程并断言这个不等式。断言 `stale_chunk_received == 2`、`stale_chunk_played == 0`，新回复开始之后 AudioSink 中没有旧 generation 的帧。另一个用例让迟到的 chunk 落在关闭流程第 3 步之后，断言关闭后 AudioSink 零写入。
- **通用断言**：所有队列都有容量上限；关闭之后没有仍在运行的任务（检查 `asyncio.all_tasks()` 中属于本 session 的任务）。
- **其他测试**：
  - 预生成作废
  - RuleTurnScorer 单元测试
  - TTS 开头底噪裁剪：首包不含底噪，词时间戳已经平移
  - 搭腔门禁："yeah" 不打断；"I see" 不会在 "I" 时被误判；"Stop." 立即打断
  - 迟到的词触发打断
  - pending 阶段用户继续说话
  - 并发 cancel 的幂等性
  - 首包超时 / 中途超时（finish 后播完已入队的部分并标记截断）
  - 背压阻塞的时间不计入 Provider 超时
  - generation 级任务异常不影响 session
  - 关闭超时（`close_timeout`、`overdue_task_count`、`reason`）
  - `noise_timeout_ms` 不等式成立

## 13. 生命周期与异常路径【用户同意】
| 情况 | 处理 |
|---|---|
| 正常完成 | 播完 → `playback_stopped(completed)` → LISTENING |
| 用户打断 | §9、§10.1 invalidate |
| Provider 首包超时 | invalidate，写 `generation_failed`，回到 LISTENING，不播兜底话术 |
| Provider 中途超时 | finish：已入队的部分照常播完，写 `playback_stopped(truncated)`，上下文只写入听到的部分 |
| generation 级任务异常 | supervisor 捕获，invalidate，session 继续运行 |
| session 级任务异常 | 关闭 session（`reason=session_task_failed`） |
| 客户端断开 | 关闭 session（`reason=client_disconnect`） |
| 多个 cancel 几乎同时到达 | 幂等（§10.3） |

**关闭顺序**（由 `Session.run()` 执行，见 §10.4）：

主动关闭和客户端断开走同一条路径，触发方式都是设置 `stop_event(reason)`：
1. 标记 CLOSING，停止接收输入。
2. invalidate 所有 generation。
3. 播放器门控切到 `closed`。此后任何写入都记 `gate=session_closed`。
4. 显式取消 session 级的循环任务：`audio_pump`、`asr_consumer`、cascade/gate 驱动、`player_ticker`。
5. 等待 session 的 TaskGroup 退出。超过 `close_grace_ms` 还有任务没退出时，**不放弃，继续等待**；同时写 `close_timeout` 告警，列出未退出的任务名，并计入 `overdue_task_count`。【用户决定】之后**每隔一个 `close_grace_ms`，就对仍未退出的任务再 `cancel()` 一次**，每次都写一条 `close_timeout`（带 `round` 轮次），专门对付吞掉了第一次取消的任务。【用户同意】
6. 丢弃各队列的残留，按队列分别记录数量。
7. 写 `session_closed`。

**异常路径**（session 级任务抛出异常）：TaskGroup 已经自动取消了所有子任务，并等待它们退出。`Session.run()` 在外层捕获异常后，依次执行第 2、3、6、7 步。其中第 2 步只做同步部分（标记作废、清除队列），因为排空用的任务已经被取消了。

`session_closed` 的 payload 包括：
   - `reason`：`explicit_close`（主动关闭）、`client_disconnect`（客户端断开）、`session_task_failed`（session 级任务异常，附异常摘要）【用户决定】
   - 各队列的残留数量和 `queue_max_depth`
   - `overdue_task_count`

第 5 步会一直等到所有任务退出，所以写 `session_closed` 时仍在运行的任务数必然为 0。

**防止卡死的约束**【用户同意】：
- **不允许吞掉 `CancelledError`**：任何任务都不能吞掉它。确实需要做清理时，用 `try/finally`，或者捕获后重新抛出。
- **核心代码不用线程**：见 §3。
- **外部取消也要完整关闭**：调用方直接取消 `Session.run()` 时，关闭序列在 `finally` 中执行，并用 `asyncio.shield` 保护，保证不会半途中断。如果当时还没写 `session_closed`，reason 记为 `explicit_close`。
- **测试**：
  - 一个"吞掉第一次取消"的假 Provider，验证"再取消一次"的路径生效：`close_timeout` 至少有 1 轮，最终 `session_closed` 写出，且仍在运行的任务数为 0。
  - 外部取消 `run()` 后，`session_closed` 仍然写出。

## 14. 实施优先级与交付物
**优先级**【我的决定，回应 CLAUDE.md 的"别做多余的"】：
- **P0**：虚拟 event loop、事件日志、BoundedQueue、假 Provider、Session 和 generation 的取消机制、播放器和 Playback Truth、判停（L1 规则打分 + 动态阈值 + 硬上限）、打断门禁、场景 A' / B / C / D / E、指标计算
- **P1**：投机预生成和场景 A、L2 / L3 升级、开头底噪裁剪、§12 中的其他测试
- **P2**：真实模型适配器
- **最后**：交付物

P1 和 P2 都不能反过来要求 P0 的接口做额外泛化。

**交付物**：README.md、DESIGN.md（2～4 页，由本文压缩而成，回答需求文档 §6.3 的全部问题以及 B～E 的补充说明）、src/、tests/、sample-output/（一次 barge-in 的 trace.jsonl、各场景指标汇总、简短分析）、agent-chat.md（移除 PII）。

## 15. 留给实现者决定的事项
- 虚拟 event loop 的实现细节（方案已在 §3 定死，满足 §12 的逐字节一致即可）。
- 模块和文件的划分、类的命名。
- 默认参数的具体数值（§16）：可以调整，但要保证场景断言、`noise_timeout_ms` 不等式和 E 的时序不等式都成立，并在 DESIGN.md 中写明最终取值。
- RuleTurnScorer 的词表和权重；开头静音的裁剪方法。
- 真实模型适配器的具体调用代码：能实例化、能通过同一套接口返回结果即可，不进入核心测试。
- 运行场景并生成 sample-output 的脚本形式。

## 16. 默认参数表【我的决定】
| 参数 | 默认值 | 约束 / 说明 |
|---|---|---|
| VAD 判定开始说话 / 说完 | 连续 3 帧语音 / 连续 4 帧静音 | `speech_end` 回溯到第一帧静音 |
| `initial_silence_ms` | 200 | |
| `final_wait_ms` | 300 | |
| `max_silence_ms` | 1500 | 必须 > 场景 B 的 700ms 停顿 |
| L1 / L2 / L3 预算 | 50 / 150 / 500 ms | |
| `high`（200ms 起点）/ `low` / `min_conf` | 0.85 / 0.3 / 0.5 | |
| `high_floor` / `threshold_floor_at_ms` | 0.5 / 1000 | `high_floor` 必须 > `low`，这样"很确定没说完"永远不会被阈值放行 |
| `speculate_threshold` | 0.6 | |
| 假 ASR `partial_latency_ms` / `final_latency_ms` | 100 / 150 | |
| 各假 Provider 最大抖动 | 20 ms | 每个 Provider 用独立的 `Random(seed)` |
| `noise_timeout_ms` | 400 | 必须 > `final_latency_ms` + 最大抖动 + asr_in 排队延迟 |
| 附和评分器预算 | 100 ms | 超时时，纯附和词按搭腔处理 |
| 假 LLM 首包 / 间隔 | 300 / 50 ms | |
| 假 TTS 首包 / 间隔 | 200 / 40 ms | 每个 chunk 约 200ms 音频 |
| LLM / TTS 首包超时 | 2000 / 1000 ms | TTS 首包超时从第一段文本送进 TTS 时开始计算 |
| chunk 间隔超时 | 1000 ms | 阻塞在下游的时间不计入 |
| `cancel_grace_ms` | 1000 | E 的测试按 §12 的不等式调大 |
| `close_grace_ms` | 2000 | 必须 > `cancel_grace_ms` |
| 队列容量 asr_in / text / playback | 50 帧 / 64 段 / 75 帧 | |
| 上下文历史 | 20 条 | |
| `playback_progress` 记录间隔 | 100 ms | |
