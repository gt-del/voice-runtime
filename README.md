# 最小 Voice Runtime（单会话）

单会话的流式语音运行时：`Audio In → VAD/Endpointing → ASR → LLM → TTS → Playback`。
全部 Provider 用确定性假实现，跑在**虚拟时钟**上，测试和 `sample-output/` 每次运行逐字节一致。

- 需求：[docs/requirements.md](docs/requirements.md)（附录 A 是面试官原文）
- 设计说明（2～4 页）：[DESIGN.md](DESIGN.md)；完整设计文档：[docs/superpowers/specs/2026-10-15-voice-runtime-design.md](docs/superpowers/specs/2026-10-15-voice-runtime-design.md)
- 样例输出：[sample-output/](sample-output/)（[指标汇总](sample-output/metrics.md)、[barge-in trace](sample-output/barge_in.trace.jsonl)、[trace 分析](sample-output/analysis.md)）

## 环境

- Python 3.12（macOS 上用 Homebrew：`brew install python@3.12`）
- 核心运行时零第三方依赖；开发依赖只有 `pytest`、`pytest-timeout`
- 真实模型适配器的依赖放在可选 extras `real` 中，跑测试不需要安装

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

> 若 `python3` 已指向 3.12，也可以直接 `python3 -m venv .venv`。

## 运行与测试

跑全部测试（适配器的真实推理测试因缺少 `real` 依赖显示 skipped）：

```bash
.venv/bin/pytest
```

把警告当错误再跑一遍（检查未关闭的资源等）：

```bash
.venv/bin/pytest -W error
```

依次跑 A'、A、B、C、D、E 六个场景，生成 trace 和指标汇总：

```bash
.venv/bin/python scripts/run_scenarios.py --out sample-output
```

输出：`<场景>.trace.jsonl`、`barge_in.trace.jsonl`（= 场景 C）、`metrics.json`、`metrics.md`。重复运行结果完全相同。

## 架构概览

```
ScriptedAudioSource ─帧─► Session（唯一 owner：ID、状态机、TaskGroup、active_generation_id）
                            ├─ VAD ─► SpeechTracker（speech_start / speech_end）
                            ├─ [asr_in 队列] ─► ASR ─► partial / final
                            ├─ EndpointCascade（L1 规则 → L2 → L3 → 静音硬上限）+ 投机预生成
                            ├─ BargeInGate（暂停 → 判定 → 恢复 / 打断）
                            └─ GenerationSupervisor(gen_id)：LLM ─闸①─► [text] ─► TTS ─裁剪─闸②─► [playback]
                                                                                  Player ─闸③─► AudioSink
所有组件 ─同步写─► EventLog（JSONL）─离线─► compute_metrics
```

| 模块（`src/voice_runtime/`） | 作用 |
|---|---|
| `vloop.py` | 虚拟时间 event loop：重写 `time()`，空闲时直接跳到下一个定时器 |
| `session.py` | Session：状态机、轮次、generation 启动 / 作废、门禁接线、关闭序列 |
| `generation.py` | GenerationSupervisor：LLM / TTS 桥接任务、闸①②、cancel 后排空与 grace 看门狗 |
| `playback.py`、`truth.py` | 播放器门控（idle / held / playing / paused / closed）、闸③、Playback Truth 账本 |
| `endpointing/` | 分级判停（`cascade.py`）与 L1 规则打分器（`rules.py`） |
| `gate.py` | 打断门禁：指令词、噪声、搭腔判定 |
| `queues.py` | `BoundedQueue`：有界、put 醒来后重新校验闸条件 |
| `events.py`、`metrics.py` | 事件常量与 JSONL 日志；只从日志离线计算指标 |
| `providers/` | Provider 接口与确定性假实现（首包延迟、间隔、抖动、卡住、异常、cancel 后迟到项） |
| `script.py`、`scenarios.py`、`runner.py` | 脚本化音频源、场景 A～E 定义、按场景构造并运行 Session |
| `trim.py` | TTS 开头底噪裁剪（能量阈值） |
| `adapters/` | 真实模型适配器（只有接口，见下文） |

## 实际耗时

> TODO（作者填写）：实际投入的时间。

## 已完成 / 未完成

已完成：

- 场景 A'、A、B、C、D、E 全部有自动化行为测试，并输出指标（[sample-output/metrics.md](sample-output/metrics.md)）
- 虚拟时钟，日志逐字节可复现（有测试断言）
- 分级判停：L1 规则打分 + 动态阈值 + 提前升级到 L2 / L3 + 静音硬上限；L2 / L3 在测试中用按脚本查表的假打分器验证，`sample-output` 只用 L1
- 投机预生成（场景 A）、TTS 开头底噪裁剪
- 打断门禁：先暂停、确认后再取消；指令词、噪声、搭腔、迟到的词
- Generation 隔离（三道闸 + purge）、有界队列与背压、Playback Truth、上下文只写入已听到的部分
- 生命周期：正常完成、打断、首包 / 中途超时、客户端断开、主动关闭、generation 级与 session 级任务异常、并发 cancel 幂等、关闭后无残留任务、关闭超时后重复取消

未完成 / 有意不做：

- **真实模型适配器（P2）只接了接口**：`adapters/` 下的 Silero VAD、FireRedChat turn detector、Smart Turn、Jev 都能实例化、符合同一套 Protocol，但**输入输出形状、张量名、接口字段都是未经验证的占位**，没有在真实模型或服务上跑过；真实时钟也没有接入，所以适配器目前不能接到 Session 上做实时 demo。
- **回声识别只写了方案**（见 DESIGN.md「已知局限」），没有实现。
- 只支持英文。
- `agent-chat.md`：由作者导出对话记录并去除个人信息后自行添加。

## AI 工具使用说明

> 本节是骨架，标注 TODO 的部分由作者本人补充。

- **用了什么、用在哪**：TODO（作者确认 / 补充）。开发过程使用 Claude Code：先讨论需求、写设计文档（`docs/superpowers/specs/`）和实施计划（`docs/superpowers/plans/`），再按计划逐任务实现、评审，最后生成本 README、DESIGN.md 和 `sample-output/` 分析。
- **哪些设计是作者自己做的**：设计文档中标注【用户决定】的条目，是讨论中由作者明确拍板的：
  - 真实模型只作为可替换的打分器、只接接口，核心测试全部用确定性假实现；适配器选型为 Silero VAD、FireRedChat-turn-detector、Smart Turn v3 / LiveKit v1-mini、Jev / LLM
  - 只支持英文；回声识别只写方案、不实现
  - Python 3.12 + asyncio；用 `python3 -m venv` + pip + `pyproject.toml` 管理依赖
  - 分级判停：按级打分，拿不准就升级到更好的模型；L1 用确定性规则打分器（方案二）
  - 级联方案（流式 ASR、流式 LLM、双流式 TTS）+ 投机预生成；预生成的存放上限与 playback 队列挂钩
  - TTS 开头静音底噪裁剪
  - 打断门禁看"持续时间 + 是否识别出词"；附和评分把话切成 80ms 小片取最大值，评分器超时 / 失败时纯附和词按搭腔处理；其余情况默认判为打断；搭腔转写直接丢弃；恢复播放从暂停样本位继续
  - 新一轮不等旧 generation 排空，立即开始
  - 三道闸，被拦下的事件必须带 `gate`
  - 关闭超时后不放弃、继续等待并记告警；`session_closed.reason` 的三种取值
  - 另有标注【用户同意】的条目（虚拟时钟、提前升级、动态阈值、预生成触发规则、生命周期与防卡死约束等）是设计者提出、作者审阅后同意的。
- **是否完全理解提交的代码**：TODO（作者本人填写）。
