# 最小 Voice Runtime —— 需求规格说明

> 状态：需求整理稿（未开始设计/实现）
> 用途：作为后续 OpenSpec 式拆解（capability → change proposal → tasks）的输入
> 来源：面试官原始题目，原文见 [附录 A](#附录-a面试官原始需求原文)
> 约定：**SHALL / MUST** = 必须；**SHOULD** = 建议/加分；**MAY** = 可选。需求编号格式 `R-<能力域>-<序号>`，验收场景用 `WHEN / THEN` 描述。

---

## 1. 背景与目标

实现一个**单会话**的最小 Voice Runtime，流水线为：

```
Audio In → VAD / Endpointing → ASR → LLM → TTS → Playback
```

评分**全部集中在 Runtime 本身**：状态、时序、并发、取消、可观测性。

### 1.1 范围内（In Scope）

- 单会话的完整流式流水线编排
- VAD / ASR / LLM / TTS 的接口设计 + 确定性 Fake Provider
- Endpointing 策略、Barge-in（打断）、噪声抗干扰
- Generation 隔离、有界队列、Playback Truth、生命周期与取消传播
- 结构化事件日志 + 场景指标
- 场景 A～E 的可重复自动化测试
- 文档交付（README / DESIGN / sample-output / agent chat）

### 1.2 范围外（Out of Scope）

- UI、WebRTC、SIP、真实模型、部署
- 多会话 / 多租户
- 语义级 endpointing 模型（明确"不要求"）
- 真实 VAD（Silero / WebRTC VAD）**可选**接入，但**不能抵扣核心部分**

---

## 2. 术语

| 术语 | 含义 |
|---|---|
| Frame | 一个 20ms 音频帧（PCM16 / 16kHz / mono = 320 samples = 640 bytes），带单调递增序号与时间戳 |
| Endpoint | 判定"用户这一轮说完了"的时刻，触发下游生成 |
| Turn | 一次用户发言 + 对应机器人回复的一轮交互 |
| Generation | 一次 LLM→TTS→Playback 的生成过程；被打断后会产生新 generation |
| Barge-in | 机器人播放过程中用户开口打断 |
| Stale event | 属于已失效（被取消/已结束）generation 的迟到事件 |
| Playback Truth | 用户**实际听到**的内容（区别于"已生成"） |
| Overlap | 用户与机器人声音实际同时存在的时长 |

---

## 3. 全局约束

| 编号 | 约束 |
|---|---|
| R-GLOBAL-01 | 音频格式 SHALL 为单声道 / PCM16 / 16kHz / 20ms 一帧 |
| R-GLOBAL-02 | 每帧 SHALL 携带单调递增的序号（sequence）与时间戳 |
| R-GLOBAL-03 | 核心测试 SHALL 可重复运行，结果确定，不依赖真人说话 |
| R-GLOBAL-04 | 所有队列 SHALL 有上限，不接受无界队列 |
| R-GLOBAL-05 | 时钟方案（真实时间 / 虚拟时钟）自行决定并说明理由；可注入虚拟时钟 SHOULD（加分项） |

---

## 4. 能力域需求

以下按能力域（对应 OpenSpec 的 capability / spec）划分。

### 4.1 Audio Input（`audio-input`）

- **R-AUDIO-01** 系统 SHALL 以帧流方式接收音频输入，帧格式符合 R-GLOBAL-01/02。
- **R-AUDIO-02** 系统 SHALL 提供可脚本化的音频源（例如"语音段 / 静音段 / 噪声段"按时间编排），用于驱动场景测试。
- **R-AUDIO-03** 每一帧 SHALL 在事件日志中可追踪（`audio frame` 事件，允许采样/聚合但需可解释）。

### 4.2 Provider 接口与 Fake 实现（`providers`）

- **R-PROV-01** VAD、ASR、LLM、TTS 的接口 SHALL 自行设计，并支持流式输出与取消。
- **R-PROV-02** 每个 Provider SHALL 有确定性的 Fake 实现。
- **R-PROV-03** Fake Provider SHALL 支持以下可配置参数：
  - 首包延迟（first-chunk latency）
  - chunk 间隔（inter-chunk interval）
  - 抖动（jitter，需确定性，如固定种子）
  - 超时（timeout / 长时间无返回）
  - 取消后仍返回 N 个迟到 chunk（late chunks after cancel）
- **R-PROV-04** 接口设计 MUST NOT 假设 `cancel()` 之后上游立刻停止（见 R-GEN-04）。
- **R-PROV-05** MAY 接入真实 VAD（Silero / WebRTC VAD），但不作为核心验收依据。

### 4.3 Endpointing（`endpointing`）

- **R-EP-01** 系统 SHALL 在用户说完一句后判定 endpoint 并提交（`endpoint committed`）。
- **R-EP-02** 用户句中犹豫停顿（如 700ms）期间 SHALL NOT 开始播放回复。
- **R-EP-03** R-EP-02 MUST NOT 仅依赖一个很大的固定静音阈值实现。
- **R-EP-04** Endpointing 策略 SHOULD 综合多种信号，候选包括：VAD、partial transcript、静音时长、句子完整度、动态阈值。（不要求语义模型）
- **R-EP-05** 真正说完后 SHALL 在合理时间内确认 endpoint（需给出量化指标 `endpoint latency`）。

**场景**
- WHEN 用户说出完整一句 "Please book it for Wednesday afternoon." 并静音
  THEN endpoint 被提交，且 endpoint latency 在策略声明的合理范围内
- WHEN 用户说 "Please book it for…"、停顿 700ms、再说 "…Wednesday afternoon."
  THEN 停顿期间无 `playback started` 事件；后半句结束后才提交 endpoint

### 4.4 流水线编排（`pipeline`）

- **R-PIPE-01** Endpoint 提交后 SHALL 依次流式驱动 ASR final → LLM → TTS → Playback。
- **R-PIPE-02** 各环节 SHALL 流式衔接，除 Provider 固有延迟外不得有明显额外排队延迟（指标 `turn-end → first-audio`）。

### 4.5 Barge-in 与噪声鲁棒性（`barge-in`）

- **R-BI-01** 机器人播放期间检测到用户真实开口，系统 SHALL：
  1. 停止播放
  2. 取消旧 generation
  3. 清空未播放队列
  4. 发起新一轮
- **R-BI-02** 从用户开口到播放停止 SHALL ≤ 250ms。
- **R-BI-03** 系统 SHALL 分别记录三段延迟：speech-start detection、interruption decision、playback stop；以及双方声音实际重叠时长（overlap duration）。
- **R-BI-04** 播放中出现一次 80ms 高能量噪声 SHALL NOT 永久中断当前回复。
- **R-BI-05** 需说明区分真实 barge-in 与噪声的策略（如最小连续语音时长），以及该保护给真实打断增加的延迟。
- **R-BI-06** 需说明是否考虑"先暂停播放、确认后再正式取消"（pause-then-confirm）方案。
- **R-BI-07** 误打断次数 SHALL 作为指标输出（`false interruption count`）。

**场景**
- WHEN 机器人播放约 4s 的回复，在 1.2s 时用户说 "Stop. Make it Friday instead."
  THEN 播放在用户开口后 ≤ 250ms 停止；旧 generation 被取消；未播队列清空；新一轮基于新输入启动
- WHEN 播放中出现一次 80ms 高能量噪声
  THEN 当前回复最终继续/完成播放，`false interruption count` 不导致永久中断

### 4.6 Generation 隔离（`generation-isolation`）

- **R-GEN-01** 每轮生成 SHALL 有明确身份（如 `session_id` / `turn_id` / `generation_id`）。
- **R-GEN-02** 需自行决定并在文档中说明：ID 何时创建、由谁持有、事件如何校验有效性、cancel 与 session close 的关系。
- **R-GEN-03** 旧 generation 的迟到事件 SHALL NOT 影响当前 generation。
- **R-GEN-04** TTS 收到 cancel 后仍吐回的旧 chunk（如 2 个）SHALL：
  - 记为 stale event（`stale event dropped`）
  - SHALL NOT 被播放
  - SHALL NOT 在新回复之后再次出现
- **R-GEN-05** Session 关闭后 SHALL NOT 再写播放器。

**场景**
- WHEN TTS 收到 cancel 后仍返回 2 个旧 chunk
  THEN 两者被记录为 stale 并丢弃；`stale_chunk_played_count == 0`；新回复播放中/之后不出现旧音频
- WHEN session 已关闭，迟到 chunk 到达
  THEN 播放器无任何写入

### 4.7 有界队列与背压（`bounded-queues`）

- **R-Q-01** 所有队列 SHALL 有容量上限，且可被测试断言。
- **R-Q-02** 需说明以下四种情况的处理策略：
  1. TTS 快于播放器
  2. ASR 慢于输入
  3. Provider 长时间无返回
  4. 关闭时队列仍有残留
- **R-Q-03** "TTS 快于播放"时需在 阻塞 / 丢弃 / 限流 / 取消 中做出选择并论证。
- **R-Q-04** 每个场景 SHALL 输出最大队列长度指标。

### 4.8 Playback Truth（`playback-truth`）

- **R-PT-01** 每个 TTS chunk 的状态 SHALL 至少区分：`generated` / `synthesized` / `enqueued` / `played`。
- **R-PT-02** 每个 TTS chunk SHALL 能关联：
  - 文本范围
  - 音频时长或 sample 数
  - 是否入队
  - 实际播放量
  - 是否被截断
- **R-PT-03** 句中被打断时 SHALL 能输出：
  - 完整生成文本
  - 入队文本
  - 用户实际听到的文本范围
  - 实际播放时长
- **R-PT-04** "切在词中间"的近似策略自定，但 SHALL 在文档中写明。
- **R-PT-05** 下一轮 LLM 上下文 SHALL NOT 无条件把完整回复当作已被听到（应使用实际听到的部分）。
- **R-PT-06** 需明确"实际播放状态"的唯一事实来源（single source of truth）。

### 4.9 生命周期与取消传播（`lifecycle`）

- **R-LC-01** 系统 SHALL 正确处理以下情况：
  - 正常完成
  - 用户打断
  - Provider 超时
  - 客户端断开
  - 主动关闭
  - 后台任务抛异常
  - 多个 cancel 几乎同时到达（幂等）
- **R-LC-02** 需说明：
  - 谁是 session 生命周期的 owner
  - 取消如何传播到各组件
  - 如何等待资源释放
  - 如何避免孤儿任务
- **R-LC-03** Session 关闭后 SHALL 没有仍在运行的后台任务（可测试断言）。

### 4.10 可观测性（`observability`）

- **R-OBS-01** 系统 SHALL 输出结构化事件日志（建议 JSONL）。
- **R-OBS-02** 每条事件 SHALL 至少包含：`timestamp` / `session_id` / `turn_id` / `generation_id` / `event_type` / `sequence_number` / `payload`。
- **R-OBS-03** 事件类型 SHALL 至少覆盖：

  | 类别 | 事件 |
  |---|---|
  | 输入 | audio frame |
  | VAD / Endpoint | speech start、speech end、endpoint committed |
  | ASR | ASR partial、ASR final |
  | LLM | LLM chunk |
  | TTS | TTS chunk |
  | 播放 | audio enqueued、playback started、playback progress、playback stopped |
  | 控制 | generation cancelled、stale event dropped、session closed |

- **R-OBS-04** 每个场景跑完 SHALL 输出以下指标：

  | 指标 | 说明 |
  |---|---|
  | endpoint latency | 用户实际说完 → endpoint committed |
  | turn-end → first-audio | endpoint → 首个音频实际播放 |
  | interruption → playback-stop | 用户开口 → 播放停止（并拆分为三段，见 R-BI-03） |
  | overlap duration | 双方声音实际重叠时长 |
  | false interruption count | 误打断次数 |
  | stale chunk received / played count | 收到 / 播放的过期 chunk 数 |
  | max queue length | 各队列最大长度 |

- **R-OBS-05** 验收标准：**仅看日志就能解释清楚这一次执行为什么是这个结果**。

---

## 5. 场景验收矩阵

| # | 输入 | 必须成立的行为 | 关键指标 | 必须的测试断言 |
|---|---|---|---|---|
| A | 完整一句 "Please book it for Wednesday afternoon." | 判定 endpoint → ASR/LLM/TTS → 开始播放；除 Provider 固有延迟外无明显额外排队 | endpoint latency、turn-end→first-audio | 播放按序发生；首包延迟 ≈ Provider 延迟之和 + 小常量 |
| B | "Please book it for…" 停 700ms → "…Wednesday afternoon." | 停顿期间不得开始播放；不能靠很大的固定静音阈值 | endpoint latency | **犹豫期间无提前播放**；**说完后合理时间内确认 endpoint** |
| C | 播放约 4s 回复，1.2s 时用户说 "Stop. Make it Friday instead." | 停播 + 取消旧 generation + 清空未播队列 + 起新一轮 | 三段延迟、overlap | **打断后 250ms 内停播**；新一轮启动；Playback Truth 输出正确的已听文本 |
| D | 播放中出现一次 80ms 高能量噪声 | 不得永久中断当前回复 | false interruption count | **80ms 噪声不造成永久中断** |
| E | TTS cancel 后仍吐 2 个旧 chunk | 记为 stale、不播放、不在新回复后出现；session 关闭后不写播放器 | stale received / played | **`stale_chunk_played_count == 0`**；关闭后播放器零写入 |
| 通用 | — | — | max queue length | **所有队列有容量上限**；**session 关闭后无运行中后台任务** |

> 测试要求：A～E 每个场景至少一个自动化测试，验证**行为**而非"没报错"。

---

## 6. 文档需回答的问题清单

### 6.1 DESIGN.md 内容结构（2～4 页）

- [ ] 架构图
- [ ] 关键状态与事件
- [ ] 并发与取消模型
- [ ] Playback Truth
- [ ] Endpointing 策略
- [ ] 取舍与已知局限

### 6.2 场景补充说明（各 3～5 句）

- [ ] **B**：endpointing 用了哪些信号（VAD / partial transcript / 静音时长 / 句子完整度 / 动态阈值）；"响应快"与"抢话"之间如何取舍
- [ ] **C**：分别记录 speech-start detection、interruption decision、playback stop 三段延迟，以及实际重叠时长
- [ ] **D**：如何区分真实 barge-in 与噪声（是否用最小连续语音时长）；该保护增加多少延迟；是否考虑"先暂停、确认后再取消"
- [ ] **E**：说明不假设 cancel() 后上游立刻停止的处理方式

### 6.3 设计问答

- [ ] 谁是"实际播放状态"的唯一事实来源？
- [ ] 为什么 TTS cancel 不足以完成一次可靠打断？
- [ ] 为什么 generated text 不能直接进下一轮上下文？
- [ ] 固定静音阈值的问题是什么？增加确认时间怎样影响 barge-in latency？
- [ ] TTS 快于播放时：阻塞、丢弃、限流还是取消？
- [ ] 拆成三个独立服务后，事件顺序和取消语义会多出哪些问题？
- [ ] 当前实现最可能在生产出的三个故障是什么？

### 6.4 其他需说明的设计决策

- [ ] 四种队列情况的处理（R-Q-02）
- [ ] ID 的创建 / 持有 / 校验 / cancel 与 close 的关系（R-GEN-02）
- [ ] Session 生命周期 owner、取消传播、资源释放等待、孤儿任务避免（R-LC-02）
- [ ] 词中截断的近似策略（R-PT-04）
- [ ] 真实时间 vs 虚拟时钟的选择理由（R-GLOBAL-05）

---

## 7. 交付物清单

```
<repo>/
├── README.md
├── DESIGN.md
├── src/
├── tests/
├── sample-output/
└── agent-chat.md        # 若使用 Agent 开发
```

### 7.1 README.md

- [ ] 环境要求
- [ ] 运行与测试命令
- [ ] 架构概览
- [ ] 实际耗时
- [ ] 已完成 / 未完成部分
- [ ] AI 工具使用说明：用了什么、用在哪、哪些设计是自己做的、是否完全理解提交的代码

### 7.2 DESIGN.md

- [ ] 见第 6 节全部内容

### 7.3 sample-output/

- [ ] 一次 barge-in 的完整 `trace.jsonl`
- [ ] 各场景指标汇总
- [ ] 一段简短分析

### 7.4 agent-chat.md

- [ ] 完整 Agent 对话记录，重点体现"你"做了哪些决策
- [ ] 移除个人 PII

### 7.5 仓库

- [ ] Git 仓库形式交付

---

## 8. 待决策项（需求留白，由我方决定）

以下是题目明确交给实现方决定的点，拆解时应各自形成一个设计决策（ADR 或 design.md 小节）：

| # | 待决策项 | 关联需求 |
|---|---|---|
| D1 | 实现语言与并发模型（如 Python asyncio / Go / Rust tokio 等） | 全局 |
| D2 | 真实时钟 vs 可注入虚拟时钟 | R-GLOBAL-05 |
| D3 | 四类 Provider 的接口形态（流式迭代器 / 回调 / channel）与取消语义 | R-PROV-01, R-PROV-04 |
| D4 | Endpointing 信号组合与动态阈值策略 | R-EP-03, R-EP-04 |
| D5 | Barge-in 判定：最小连续语音时长、是否 pause-then-confirm | R-BI-05, R-BI-06 |
| D6 | ID 体系与事件有效性校验方式 | R-GEN-02 |
| D7 | 各队列容量与满时策略（阻塞 / 丢弃 / 限流 / 取消） | R-Q-02, R-Q-03 |
| D8 | 播放状态唯一事实来源、词中截断近似策略 | R-PT-04, R-PT-06 |
| D9 | Session 生命周期 owner 与取消传播结构（如 task group / structured concurrency） | R-LC-02 |
| D10 | 事件日志 schema 与指标计算方式（从日志离线计算 vs 运行时统计） | R-OBS-* |

---

## 9. 建议拆解顺序（供 OpenSpec change 规划参考）

按依赖关系，从底层到上层：

1. **foundation**：项目骨架、时钟抽象（D2）、事件日志与 ID 体系（R-OBS-01/02、R-GEN-01）、有界队列原语（R-Q-01）
2. **audio-input + providers**：帧模型、脚本化音频源、四类 Fake Provider 及可配置参数（R-AUDIO-*、R-PROV-*）
3. **pipeline + endpointing**：主流水线编排与 endpointing 策略 → 场景 A、B
4. **playback-truth**：播放器、chunk 状态机、已听文本计算（R-PT-*）
5. **barge-in + generation-isolation**：打断、取消、stale 过滤、噪声抗干扰 → 场景 C、D、E
6. **lifecycle**：异常 / 超时 / 断开 / 并发 cancel / 关闭无孤儿任务（R-LC-*）
7. **metrics + sample-output**：指标汇总、trace 产出、分析
8. **docs**：README、DESIGN.md、agent-chat.md

---

## 附录 A：面试官原始需求原文

> 以下为原文完整保留，用于追溯；若本文档与原文有出入，以原文为准。

### 1. 题目

实现一个单会话的最小 Voice Runtime：

Audio In → VAD/Endpointing → ASR → LLM → TTS → Playback

不要求做 UI、WebRTC、SIP、真实模型或部署。全部评分集中在 Runtime 的状态、时序、并发、取消与可观测性。

音频约定：单声道 / PCM16 / 16kHz / 20ms 一帧，每帧带单调递增的序号与时间戳。

VAD、ASR、LLM、TTS 接口自行设计，用确定性 Fake Provider 实现，且必须可配置：首包延迟、chunk 间隔、抖动、超时、取消后仍返回 N 个迟到 chunk。可选接入 Silero / WebRTC VAD 等真实组件，但不加分抵扣核心部分。

核心测试必须可重复运行，不依赖真人说话。

### 2. 必须支持的场景

| # | 输入 | 必须成立的行为 |
|---|---|---|
| A | 完整一句 "Please book it for Wednesday afternoon." | 判定 endpoint → ASR/LLM/TTS → 开始播放；除 Provider 固有延迟外无明显额外排队 |
| B | "Please book it for…" 停 700ms → "…Wednesday afternoon." | 停顿期间不得开始播放；但不能靠一个很大的固定静音阈值来实现 |
| C | 机器人播放约 4s 的回复，1.2s 时用户说 "Stop. Make it Friday instead." | 停播 + 取消旧 generation + 清空未播队列 + 起新一轮；用户开口到停播 ≤ 250ms |
| D | 播放中出现一次 80ms 高能量噪声 | 不得因此永久中断当前回复 |
| E | TTS 收到 cancel 后仍吐回 2 个旧 chunk | 记为 stale event、不得播放、不得在新回复后再次出现；session 关闭后不得再写播放器 |

补充说明（写进 DESIGN.md，各 3～5 句即可）：

- B：你的 endpointing 策略用了哪些信号（VAD / partial transcript / 静音时长 / 句子完整度 / 动态阈值），以及"响应快"与"抢话"之间怎么取舍。不要求语义模型。
- C：分别记录 speech-start detection、interruption decision、playback stop 三段延迟，以及双方声音实际重叠时长。
- D：如何区分真实 barge-in 与噪声（是否用最小连续语音时长），这项保护给真实打断加了多少延迟，是否考虑过"先暂停播放、确认后再正式取消"。
- E：不得假设 cancel() 之后上游立刻停止。

### 3. 核心设计要求

**3.1 流式 + 有限队列。** 所有队列必须有上限，不接受无界队列。说明四种情况的处理：TTS 快于播放器、ASR 慢于输入、Provider 长时间无返回、关闭时队列仍有残留。

**3.2 Generation 隔离。** 每轮生成有明确身份（如 session_id / turn_id / generation_id）。旧 generation 的迟到事件不得影响当前 generation。自行决定 ID 何时创建、由谁持有、事件如何校验有效性，以及 cancel 与 session close 的关系。

**3.3 Playback Truth。** "已生成"≠"用户已听到"。至少区分 generated / synthesized / enqueued / played。每个 TTS chunk 需能关联：文本范围、音频时长或 sample 数、是否入队、实际播放量、是否被截断。句中被打断时要能输出：完整生成文本 / 入队文本 / 用户实际听到的文本范围 / 实际播放时长。切在词中间的近似策略自定，但要写明。下一轮上下文不得无条件把完整回复当作已被听到。

**3.4 生命周期与取消传播。** 至少正确处理：正常完成、用户打断、Provider 超时、客户端断开、主动关闭、后台任务抛异常、多个 cancel 几乎同时到达。说明：谁是 session 生命周期的 owner、取消如何传播到各组件、如何等待资源释放、如何避免孤儿任务。

### 4. 可观测性

结构化事件日志（建议 JSONL），每条至少含 timestamp / session_id / turn_id / generation_id / event_type / sequence_number / payload，覆盖：audio frame、speech start/end、endpoint committed、ASR partial/final、LLM chunk、TTS chunk、audio enqueued、playback started/progress/stopped、generation cancelled、stale event dropped、session closed。

每个场景跑完输出指标：endpoint latency、turn-end→first-audio、interruption→playback-stop、overlap duration、false interruption count、stale chunk received / played count、最大队列长度。

标准：能只看日志就解释清楚这一次执行为什么是这个结果。

### 5. 测试

场景 A～E 各至少一个自动化测试，验证行为而非"没报错"。必须包含的断言：

- 犹豫场景没有提前播放；真正说完后能在合理时间内确认 endpoint
- 打断后 250ms 内停播；80ms 噪声不造成永久中断
- 被取消 generation 的迟到音频不播放，stale_chunk_played_count == 0
- 所有队列都有容量上限
- session 关闭后没有仍在运行的后台任务

真实时间还是虚拟时钟由你决定，说明理由（可注入的虚拟时钟是加分项）。

### 6. 交付

Git 仓库：README.md / DESIGN.md / src/ / tests/ / sample-output/

- README：环境、运行与测试命令、架构概览、实际耗时、已完成与未完成的部分；以及 AI 工具使用说明（用了什么、用在哪、哪些设计是你自己做的、你是否完全理解提交的代码）。
- DESIGN.md（2～4 页）：架构图、关键状态与事件、并发与取消模型、Playback Truth、Endpointing 策略、取舍与已知局限。并回答：
  - 谁是"实际播放状态"的唯一事实来源？
  - 为什么 TTS cancel 不足以完成一次可靠打断？
  - 为什么 generated text 不能直接进下一轮上下文？
  - 固定静音阈值的问题是什么？增加确认时间怎样影响 barge-in latency？
  - TTS 快于播放时：阻塞、丢弃、限流还是取消？
  - 拆成三个独立服务后，事件顺序和取消语义会多出哪些问题？当前实现最可能在生产出的三个故障是什么？
- sample-output/：一次 barge-in 的完整 trace.jsonl、各场景指标汇总、一段简短分析。
- Agent chat：如果你使用 Agent 开发，我们希望你提供完整的 Agent chat 记录（例如 agent-chat.md）。我们主要想了解"你"在这里做了哪些决策（注意移除自己的 PII）。
