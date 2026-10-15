# 最小 Voice Runtime 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 Python asyncio 实现一个单会话 Voice Runtime（Audio In → VAD/Endpointing → ASR → LLM → TTS → Playback），全部跑确定性的假 Provider 和虚拟时钟。场景 A～E 都要有行为级的自动化测试，每次运行都能输出 JSONL trace 和指标。

**Architecture:** Session 是唯一的协调者，持有所有 ID、状态机、`active_generation_id` 和 TaskGroup。各阶段之间用自定义的 `BoundedQueue` 流式衔接。每个 generation 由一个 `GenerationSupervisor` 承载，数据要依次经过三道闸，每道闸都会校验 generation 身份。播放器的帧写入是 Playback Truth 的唯一事实来源。所有组件把事件同步写入 JSONL，指标只从日志离线计算。时间由 event loop 级别的虚拟时钟驱动。

**Tech Stack:** Python 3.12（Homebrew，`/opt/homebrew/opt/python@3.12/libexec/bin/python3`）、asyncio、`python3 -m venv` + pip + `pyproject.toml`、pytest、pytest-timeout。核心代码不依赖任何第三方运行时库。

**Spec:** [docs/superpowers/specs/2026-10-15-voice-runtime-design.md](../specs/2026-10-15-voice-runtime-design.md)（下文用"设计 §N"引用）；需求 [docs/requirements.md](../../requirements.md)。

## Global Constraints

- Python ≥ 3.12；包名 `voice_runtime`，源码放在 `src/voice_runtime/`，测试放在 `tests/`。
- 依赖：核心运行时**零第三方依赖**；dev extras 只有 `pytest`、`pytest-timeout`；真实模型的依赖只放在 extras `real` 里（Task 18）。
- 音频：单声道 / PCM16 小端 / 16kHz / 20ms 一帧，即 320 samples、640 bytes。帧带 `seq`（从 0 起单调递增）和 `ts_ms`。
- **只用虚拟时钟**：所有时间都来自 `voice_runtime.vloop.now_ms()`，也就是 `loop.time()` 换算成毫秒。src 下**禁止**出现 `time.time`、`time.monotonic`、`datetime.now`、`to_thread`、`run_in_executor`、`threading`（Task 14 有一个测试用 grep 断言）。
- 每个假 Provider 各用自己的 `random.Random(seed)`。
- 所有 `asyncio.create_task` / `TaskGroup.create_task` 都必须显式传 `name=`。命名格式为 `session/<loop>` 或 `gen-<id>/<role>`。日志里只写任务名和 `f"{type(e).__name__}: {e}"`。
- **任何代码都不得吞掉 `CancelledError`**：`except BaseException` 和 `except asyncio.CancelledError` 捕获后都必须重新抛出。
- 所有队列都用 `BoundedQueue`，禁止使用 `asyncio.Queue()` 和无界的 `list`/`deque` 作缓冲。上下文历史用 `deque(maxlen=20)`。
- 事件 JSON 用 `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`，每行一条，同步追加写入。
- 默认参数一律按设计 §16 的取值，集中写在 `RuntimeConfig` 里（Task 2）。
- 运行测试：`.venv/bin/pytest`。pyproject 中配置 `timeout = 30`（真实时间），所以测试卡住时会失败退出，而不是一直挂着。
- 提交信息用中文、Conventional Commits 格式，末尾加一行 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。
- 遵守 CLAUDE.md：不做计划之外的功能；不为只用一次的代码抽象；回复和文档使用简体中文。

## Review Focus

以下五种输入或故障，规格里没有专门写测试，但最容易在真实使用中出问题。对应的测试已经分别加到负责的任务里：

1. **连续两次打断**：新回复刚开始播放，用户又打断一次。期望第二次也能在 250ms 内停播；两代旧 generation 的迟到 chunk 一个都不播放。测试放在 Task 13。
2. **LLM 回复为空**（0 个 chunk 就 EOS）。期望不写 `playback_started`；这一轮以 `completed` 结束，只把用户文本写进上下文；session 回到 LISTENING。测试放在 Task 12。
3. **ASR 卡住，用户还在一直说**。期望 `asr_in` 丢弃最旧的帧并写 `queue_overflow`，深度不超过 50；VAD 和播放不受影响，session 不死锁。测试放在 Task 12。
4. **重复调用 `close()`，或者在 paused 状态下关闭**。期望只写一条 `session_closed`；播放器零写入；`run()` 正常返回。测试放在 Task 14。
5. **判停前后 final 改写了文本**（partial 是 "book it for wednesday"，final 是 "Book it for Wednesday afternoon."）。期望旧文本的打分记 `stale_event_dropped(gate=endpoint)`，判停用的是 final 的文本。测试放在 Task 8。

---

## 文件结构

```
pyproject.toml                         # 包元数据、dev/real extras、pytest 配置
src/voice_runtime/
  __init__.py
  vloop.py          # 虚拟时间 event loop、run()、now_ms()
  config.py         # RuntimeConfig（设计 §16 全部默认值）
  events.py         # EventLog（同步写 JSONL + 监听器）、read_jsonl()、事件类型常量
  queues.py         # BoundedQueue（背压 / 丢最旧 / 醒来重校验 / purge / max_depth）
  audio.py          # Frame、AudioChunk、WordTiming、PCM 工具函数
  script.py         # 脚本段落类型、ScriptedAudioSource（会写 script_marker）
  providers/
    __init__.py
    base.py         # Protocol：VAD/ASR/LLM/TTS/TurnScorer/BackchannelScorer；ProviderStream；数据类
    fake.py         # 假实现与 StreamFaults
  speech.py         # SpeechTracker（3/4 帧规则，speech_end 回溯，epoch）
  endpointing/
    __init__.py
    rules.py        # RuleTurnScorer（L1）
    cascade.py      # EndpointCascade（动态阈值、升级、stale 判定）
  truth.py          # TruthLedger：chunk 状态、已播放帧、听到的文本
  playback.py       # Player（门控、tick、闸③、EOS、欠载、progress）、AudioSink、FakeSink
  generation.py     # Generation、GenerationSupervisor（闸①②、invalidate/finish、排空、超时）
  trim.py           # 开头底噪裁剪（P1）
  gate.py           # BargeInGate 判定规则（纯逻辑）
  session.py        # Session：编排、状态机、预生成、关闭序列
  metrics.py        # compute_metrics(events)
  scenarios.py      # 场景 A'/A/B/C/D/E 的脚本与配置（测试和 run_scenarios 共用）
  adapters/         # P2：真实模型适配器（可选依赖）
scripts/run_scenarios.py               # 生成 sample-output/
tests/
  helpers.py                           # run_scenario() 测试工具
  test_*.py                            # 各任务的测试
sample-output/  README.md  DESIGN.md  agent-chat.md
```

---

## P0

### Task 1: 项目骨架与虚拟时间 event loop

**Files:**
- Create: `pyproject.toml`、`src/voice_runtime/__init__.py`、`src/voice_runtime/vloop.py`
- Test: `tests/test_vloop.py`
- Modify: `.gitignore`（追加 `*.egg-info/`、`.pytest_cache/`、`/runs/`）

**Interfaces:**
- Produces:
  - `class VirtualTimeLoop(asyncio.SelectorEventLoop)`
  - `def run(coro: Coroutine[Any, Any, T]) -> T`：新建一个 VirtualTimeLoop，从虚拟时间 0 开始运行，结束后关闭 loop
  - `def now_ms() -> int`：`round(asyncio.get_running_loop().time() * 1000)`
  - `class VirtualLoopStalled(RuntimeError)`

- [ ] **Step 1: 建 venv、写 pyproject 并安装**

`pyproject.toml` 使用 setuptools 的 src 布局，`requires-python = ">=3.12"`，`[project.optional-dependencies] dev = ["pytest", "pytest-timeout"]`、`real = []`（Task 18 再填），`[tool.pytest.ini_options] timeout = 30, testpaths = ["tests"]`。

Run: `/opt/homebrew/opt/python@3.12/libexec/bin/python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`
Expected: 安装成功，`.venv/bin/python --version` 输出 `Python 3.12.x`

- [ ] **Step 2: 写失败的测试 `tests/test_vloop.py`**

```python
def test_sleep_advances_virtual_time_instantly():
    async def main():
        t0 = now_ms(); await asyncio.sleep(3600); return now_ms() - t0
    wall = time.monotonic()
    assert run(main()) == 3_600_000
    assert time.monotonic() - wall < 1.0

def test_timeout_fires_in_virtual_time():
    async def main():
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.25):
                await asyncio.sleep(10)
        return now_ms()
    assert run(main()) == 250

def test_interleaving_is_deterministic():
    async def main():
        order = []
        async def w(name, d):
            for _ in range(3):
                await asyncio.sleep(d); order.append((name, now_ms()))
        async with asyncio.TaskGroup() as tg:
            tg.create_task(w("a", 0.02), name="a"); tg.create_task(w("b", 0.03), name="b")
        return order
    assert run(main()) == run(main())

def test_idle_without_timers_raises_instead_of_hanging():
    async def main():
        await asyncio.Event().wait()
    with pytest.raises(VirtualLoopStalled):
        run(main())
```

- [ ] **Step 3: 运行，确认失败**

Run: `.venv/bin/pytest tests/test_vloop.py -v`
Expected: FAIL（ImportError）

- [ ] **Step 4: 实现 `vloop.py`**

做法：重写 `time()`，让它返回 `self._vt`。把 `self._selector` 包一层：它的 `select(timeout)` 先调用真实 selector 的 `select(0)`；如果没有就绪事件且 `timeout > 0`，就把 `_vt` 加上 `timeout`，然后返回空列表；如果 `timeout is None`（没有定时器，也没有就绪回调），就抛出 `VirtualLoopStalled`。`run()` 内部调用 `loop.run_until_complete`，并在 `finally` 中关闭 loop。

- [ ] **Step 5: 运行，确认通过**

Run: `.venv/bin/pytest tests/test_vloop.py -v`
Expected: 4 passed

- [ ] **Step 6: 提交**

```bash
git add pyproject.toml .gitignore src/voice_runtime/__init__.py src/voice_runtime/vloop.py tests/test_vloop.py
git commit -m "feat(vloop): event loop 级虚拟时钟与项目骨架" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: RuntimeConfig 与事件日志

**Files:**
- Create: `src/voice_runtime/config.py`、`src/voice_runtime/events.py`
- Test: `tests/test_events.py`

**Interfaces:**
- Consumes: `now_ms()`（Task 1）
- Produces:
  - `@dataclass(frozen=True) class RuntimeConfig`，字段及默认值如下（来自设计 §16）：
    - VAD 与判停：`vad_start_frames=3, vad_end_frames=4, initial_silence_ms=200, final_wait_ms=300, max_silence_ms=1500, l1_budget_ms=50, l2_budget_ms=150, l3_budget_ms=500, high_start=0.85, low=0.3, min_conf=0.5, high_floor=0.5, threshold_floor_at_ms=1000, speculate_threshold=0.6, speculation_enabled=True`
    - 打断门禁：`pause_min_speech_frames=3, noise_timeout_ms=400, backchannel_budget_ms=100, backchannel_threshold=0.5`
    - 超时与关闭：`llm_first_timeout_ms=2000, tts_first_timeout_ms=1000, inter_chunk_timeout_ms=1000, cancel_grace_ms=1000, close_grace_ms=2000`
    - 容量：`asr_in_capacity=50, text_capacity=64, playback_capacity_frames=75, context_max_messages=20`
    - 其他：`progress_interval_ms=100, l2_audio_ring_ms=3000, backchannel_audio_max_ms=2000, max_trim_ms=300, trim_enabled=True, log_audio_frames=True`
  - `class EventLog`：
    - `__init__(self, path: Path, session_id: str)`：打开文件，以追加、行缓冲方式写入
    - `emit(self, event_type: str, *, turn_id: int | None = None, generation_id: int | None = None, **payload) -> int`：返回 `sequence_number`
    - `add_listener(self, fn: Callable[[dict], None]) -> None`
    - `close(self) -> None`
  - `def read_jsonl(path: Path) -> list[dict]`
  - 事件类型常量：按设计 §11.3 列出的每个事件类型，在 `events.py` 里定义成全大写的字符串常量（如 `SPEECH_START = "speech_start"`），并提供 `ALL_EVENT_TYPES: frozenset[str]`

- [ ] **Step 1: 写失败的测试**

```python
def test_emit_writes_required_fields_and_increments_seq(tmp_path):
    def body():
        log = EventLog(tmp_path / "t.jsonl", "s1")
        a = log.emit("speech_start", turn_id=1, x=1)
        b = log.emit("speech_end", turn_id=1, generation_id=2)
        log.close(); return a, b
    a, b = run(_async(body))
    rows = read_jsonl(tmp_path / "t.jsonl")
    assert (a, b) == (0, 1)
    assert set(rows[0]) == {"timestamp", "session_id", "turn_id", "generation_id", "event_type", "sequence_number", "payload"}
    assert rows[0]["payload"] == {"x": 1} and rows[1]["generation_id"] == 2

def test_listener_sees_each_event_synchronously(tmp_path): ...  # 每 emit 一次，监听器就收到同一个 dict 一次
def test_required_event_types_are_declared():
    assert {"audio_frame","speech_start","speech_end","endpoint_committed","asr_partial","asr_final","llm_chunk",
            "tts_chunk","audio_enqueued","playback_started","playback_progress","playback_stopped",
            "generation_cancelled","stale_event_dropped","session_closed"} <= ALL_EVENT_TYPES
def test_config_defaults_match_design():
    c = RuntimeConfig()
    assert (c.max_silence_ms, c.noise_timeout_ms, c.playback_capacity_frames, c.cancel_grace_ms, c.close_grace_ms) == (1500, 400, 75, 1000, 2000)
    assert c.high_floor > c.low
```

（`_async(fn)` 是一个本地小函数：把同步函数包成协程交给 `run()`，这样 `now_ms()` 在 loop 内部有效。）

- [ ] **Step 2: 运行，确认失败**（`.venv/bin/pytest tests/test_events.py -v`，Expected: ImportError）
- [ ] **Step 3: 实现 `config.py` 与 `events.py`**。`timestamp` 取 `now_ms()`；序列化方式见 Global Constraints；监听器在写文件之后同步调用。
- [ ] **Step 4: 运行，确认通过**（Expected: 4 passed）
- [ ] **Step 5: 提交**（`feat(events): RuntimeConfig 与同步 JSONL 事件日志`）

---

### Task 3: BoundedQueue

**Files:**
- Create: `src/voice_runtime/queues.py`
- Test: `tests/test_queues.py`

**Interfaces:**
- Produces: `class BoundedQueue(Generic[T])`
  - `__init__(self, name: str, capacity: int, *, overflow: Literal["block", "drop_oldest"] = "block")`
  - `async put(self, item: T, accept: Callable[[T], bool] = lambda _: True) -> bool`
    - 等到有空位后，在**同一个同步步骤里**调用 `accept(item)`：返回 True 就写入并返回 True，返回 False 就不写入并返回 False。
    - 对于 `drop_oldest` 模式，`put` 从不阻塞：满了就丢掉最旧的一项，`dropped_count += 1`。
    - 队列已关闭时，`put` 返回 False。
  - `async get(self) -> T`：队列关闭且为空时抛 `QueueClosed`
  - `def purge(self, pred: Callable[[T], bool]) -> int`：同步删除所有匹配的项，唤醒阻塞的 put，返回删除了几项
  - `def close(self) -> int`：关闭队列，唤醒所有等待者，丢弃残留，返回丢弃的项数
  - 属性：`depth`、`max_depth`、`capacity`、`dropped_count`、`name`
- Produces: `class QueueClosed(Exception)`

- [ ] **Step 1: 写失败的测试**

```python
def test_put_blocks_when_full_until_get(): ...           # capacity=2，第 3 次 put 要等一次 get 之后才完成；max_depth == 2
def test_accept_rechecked_after_wake():
    async def main():
        q = BoundedQueue("p", 1); await q.put("old")
        valid = {"gen": 1}
        t = asyncio.create_task(q.put("x", accept=lambda _: valid["gen"] == 1), name="t")
        await asyncio.sleep(0); valid["gen"] = 2; await q.get()
        return await t, q.depth
    assert run(main()) == (False, 0)
def test_purge_removes_matching_and_wakes_putters(): ...  # purge 返回匹配的项数，阻塞的 put 随之完成
def test_drop_oldest_never_blocks_and_counts(): ...       # capacity=3，put 5 次 → depth 3，dropped_count 2，留下的是最后 3 项
def test_close_returns_residual_and_get_raises(): ...     # close() 返回残留数；get 抛 QueueClosed；put 返回 False
def test_capacity_must_be_positive():
    with pytest.raises(ValueError): BoundedQueue("x", 0)
```

- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**：内部用 `collections.deque` 存数据（其长度由 capacity 约束），配合 `asyncio.Condition` 或一组 Future 等待者；`put` 醒来后的重校验必须和写入在同一次同步执行里完成，中间不能有 await。
- [ ] **Step 4: 运行，确认通过**（6 passed）
- [ ] **Step 5: 提交**（`feat(queues): 有界队列（背压、丢最旧、醒来重校验、purge）`）

---

### Task 4: 音频模型与脚本化音频源

**Files:**
- Create: `src/voice_runtime/audio.py`、`src/voice_runtime/script.py`
- Test: `tests/test_script.py`

**Interfaces:**
- Consumes: `EventLog`、`now_ms`
- Produces（`audio.py`）：
  - 常量：`SAMPLE_RATE=16000`、`FRAME_MS=20`、`FRAME_SAMPLES=320`
  - `@dataclass(frozen=True) class Frame: seq: int; ts_ms: int; pcm: bytes`
  - `@dataclass(frozen=True) class WordTiming: word: str; start_ms: int; end_ms: int`（时间相对 chunk 开头）
  - `@dataclass class AudioChunk: pcm: bytes; text_range: tuple[int, int]; word_timings: list[WordTiming]; trimmed_samples: int = 0`
  - `def tone(ms: int, amplitude: int) -> bytes`：确定性的方波 PCM
  - `def rms(pcm: bytes) -> float`
  - `def word_duration_ms(word: str) -> int`：`50 + 10 * len(去掉标点后的 word)`，假 ASR、假 TTS、脚本共用这个函数
- Produces（`script.py`）：
  - 段落类型：
    - `Speech(text: str, amplitude: int = 3000)`：逐词按 `word_duration_ms` 排列，词与词之间无间隙
    - `Silence(ms: int)`
    - `Noise(ms: int, amplitude: int = 12000)`
    - `AfterEvent(event_type: str, delay_ms: int, nth: int = 1)`：持续发静音，直到第 nth 个 `event_type` 事件出现后再过 `delay_ms`
    - `Disconnect()`
  - `class ScriptedAudioSource`：
    - `__init__(self, segments: list, log: EventLog)`
    - `async frames(self) -> AsyncIterator[Frame]`：每 20ms `asyncio.sleep(0.02)` 一次再产出一帧。脚本跑完后持续产出静音帧；遇到 `Disconnect` 就结束迭代
    - `disconnected: bool`
  - `script_marker` 事件的 payload：`{eval_only: True, segment: "speech"|"noise"|"silence"|"disconnect", phase: "start"|"end", index, text}`，在段落的第一帧和最后一帧之后写入
  - `def speech_word_times(text: str, start_ms: int) -> list[WordTiming]`：给假 ASR 用

- [ ] **Step 1: 写失败的测试**

```python
def test_frames_are_320_samples_with_monotonic_seq_and_ts(tmp_path): ...  # 前 10 帧：len(pcm)==640，seq 为 0..9，ts 为 0,20,...
def test_markers_record_true_speech_boundaries(tmp_path):
    # [Silence(100), Speech("Please book it.")] → speech start 标记在 ts=100，end 标记在 100 + 三个词的时长之和
def test_after_event_waits_for_runtime_event(tmp_path):
    # [AfterEvent("playback_started", 1200), Noise(80)]：在 ts=500 emit playback_started → noise start 标记在 ts=1700
def test_disconnect_ends_iteration_and_silence_continues_otherwise(tmp_path): ...
def test_speech_louder_than_threshold_noise_louder_still():
    assert rms(tone(20, 12000)) > rms(tone(20, 3000)) > 1000 > rms(tone(20, 0))
```

- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**。`AfterEvent` 通过 `log.add_listener` 记录目标事件出现的时刻。
- [ ] **Step 4: 运行，确认通过**
- [ ] **Step 5: 提交**（`feat(script): 帧模型与脚本化音频源（script_marker 真值）`）

---

### Task 5: Provider 协议与假实现

**Files:**
- Create: `src/voice_runtime/providers/__init__.py`、`providers/base.py`、`providers/fake.py`
- Test: `tests/test_fake_providers.py`

**Interfaces:**
- Consumes: `Frame`、`AudioChunk`、`WordTiming`、`word_duration_ms`、`speech_word_times`、`Speech/Noise`（Task 4）
- Produces（`base.py`）：
  - `@dataclass(frozen=True) class AsrWord: w: str; start_ms: int; end_ms: int`
  - `@dataclass(frozen=True) class AsrEvent: kind: Literal["partial","final"]; text: str; words: tuple[AsrWord, ...]; segment_id: int; segment_start_ms: int; segment_end_ms: int`
  - `@dataclass(frozen=True) class TurnScore: p_complete: float; confidence: float; rules: tuple[str, ...] = ()`
  - `class ProviderStream(Protocol[T])`：实现 `__aiter__/__anext__`，另有 `def cancel(self) -> None`
  - Protocol：
    - `VAD.process(frame: Frame) -> float`
    - `ASR.run(frames: AsyncIterator[Frame]) -> AsyncIterator[AsrEvent]`
    - `LLM.start(messages: list[dict]) -> ProviderStream[str]`
    - `TTS.start(text_stream: AsyncIterator[str]) -> ProviderStream[AudioChunk]`
    - `TurnScorer.score(text: str, audio: bytes | None = None) -> Awaitable[TurnScore]`
    - `BackchannelScorer.score(slices: list[bytes]) -> Awaitable[list[float]]`
- Produces（`fake.py`）：
  - `@dataclass(frozen=True) class StreamFaults`：`first_delay_ms: int; interval_ms: int; jitter_ms: int = 20; seed: int = 0; hang_after: int | None = None; raise_at: int | None = None; late_after_cancel_ms: tuple[int, ...] = (); swallow_first_cancel: bool = False`
  - `FakeVAD(threshold_rms: float = 1000.0)`：`process` 返回 0.9 或 0.05
  - `FakeASR(segments: list, *, partial_latency_ms=100, final_latency_ms=150, jitter_ms=20, seed=1, hang_at_ms: int | None = None)`
    - 根据脚本段落和帧的 `ts_ms` 计算每个词的结束时刻和每个段落的结束时刻
    - 处理某一帧时，如果某个事件的到期时刻 ≤ 该帧的 ts，就把它吐出来（延迟按帧对齐到 20ms）
    - 噪声段的 final 文本为 `""`；final 文本使用脚本原文（保留标点），partial 去掉标点
  - `FakeLLM(replies: dict[str, str], default_reply: str, faults: StreamFaults)`：按规范化后的最后一条 user 消息查回复，逐词作为 chunk 吐出（词后带一个空格）
  - `FakeTTS(faults: StreamFaults, leading_noise_ms: int = 0, amplitude: int = 3000)`
    - 消费 text_stream，按完整的词累积
    - 每凑满约 200ms 音频（或者输入 EOS）就吐一个 chunk
    - 首包延迟从**第一段文本到达**时开始计算
    - 每次 `start` 只在第一个 chunk 前面加上 `leading_noise_ms` 的低幅底噪（amplitude 200）
  - `ScriptedTurnScorer(table: dict[str, TurnScore], default: TurnScore, delay_ms: int = 0, hang: bool = False, raise_exc: bool = False)`：表的 key 是规范化后的文本
  - `FakeBackchannelScorer(by_slice_count: Callable[[int], list[float]] | None = None, default_score: float = 0.8, delay_ms: int = 0, fail: bool = False)`
  - `def normalize_text(s: str) -> str`：转小写、去掉标点、合并空白（设计 §7 的规范化规则，Session 也用它）
- 流的 `cancel()` 语义：
  - 设置 cancelled 标志之后，只再吐出 `late_after_cancel_ms` 指定的那几项。每一项在 cancel 之后的对应毫秒数到达，到达时刻相对 cancel 计算。吐完就 `StopAsyncIteration`。
  - `hang_after` 的卡住通过 `await asyncio.Event().wait()` 实现，因此可以被取消。
  - `swallow_first_cancel=True` 时，第一次收到 `CancelledError` 会吞掉并继续等待，第二次才真正退出。这是唯一允许吞掉取消的地方，只用于 Task 14 的测试。

- [ ] **Step 1: 写失败的测试**

```python
def test_fake_llm_first_delay_interval_and_jitter_are_seeded(): ...    # 同一个 seed 两次运行，各 chunk 的到达时刻完全相同；首包 ∈ [300, 320]
def test_cancel_then_late_chunks_arrive_at_configured_offsets():
    # late_after_cancel_ms=(50, 900)：cancel 后恰好再收到 2 项，到达时刻为 cancel+50 和 cancel+900，然后流结束
def test_hang_after_is_cancellable(): ...
def test_raise_at_raises_runtime_error(): ...
def test_fake_tts_first_chunk_delay_counts_from_first_text(): ...      # 文本在 t=500 才到 → 首个 chunk 在 ≥ 700 到达
def test_fake_tts_chunk_word_timings_cover_text_range(): ...           # 拼接所有 chunk 的 text_range 能还原原文；样本数等于各词时长之和
def test_fake_asr_partials_finals_and_empty_final_for_noise(): ...     # Speech("Stop. Make it") → partial 依次为 "stop" 等；Noise(80) → final 文本为 ""
def test_fake_vad_uses_energy_not_labels(): ...
def test_normalize_text():
    assert normalize_text("  Please, book IT for Wednesday!") == "please book it for wednesday"
```

- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行，确认通过**
- [ ] **Step 5: 提交**（`feat(providers): Provider 协议与可配置确定性假实现`）

---

### Task 6: SpeechTracker

**Files:**
- Create: `src/voice_runtime/speech.py`
- Test: `tests/test_speech.py`

**Interfaces:**
- Consumes: `RuntimeConfig`、`Frame`
- Produces：
  - `@dataclass(frozen=True) class SpeechEdge: kind: Literal["start","end"]; ts_ms: int; epoch: int`
  - `class SpeechTracker`：
    - `__init__(self, cfg: RuntimeConfig)`
    - `feed(self, frame: Frame, prob: float) -> SpeechEdge | None`
    - 属性：`in_speech: bool`、`epoch: int`、`speech_frames_run: int`（当前连续语音帧数，门禁用它判断是否满 3 帧）、`last_speech_end_ms: int | None`
  - 规则：连续 `vad_start_frames` 帧语音判为 start，ts 取这一串语音的第一帧，epoch 加 1；连续 `vad_end_frames` 帧静音判为 end，ts **回溯到第一帧静音**。

- [ ] **Step 1: 写失败的测试**：
  - 3 帧语音才触发 start，2 帧不触发
  - start 的 ts 等于第一帧语音的 ts
  - end 在第 4 帧静音时触发，ts 等于第一帧静音的 ts
  - 80ms 噪声（4 帧）先触发 start，再触发 end
  - 每次 start，epoch 加 1
- [ ] **Step 2～4: 运行失败 → 实现 → 运行通过**
- [ ] **Step 5: 提交**（`feat(speech): 语音段边界与 silence epoch`）

---

### Task 7: RuleTurnScorer（L1）

**Files:**
- Create: `src/voice_runtime/endpointing/__init__.py`、`endpointing/rules.py`
- Test: `tests/test_rules.py`

**Interfaces:**
- Consumes: `TurnScore`、`normalize_text`
- Produces: `class RuleTurnScorer`，带 `async score(self, text: str, audio: bytes | None = None) -> TurnScore`（同步计算、立即返回）。`TurnScore.rules` 列出命中规则的名字，取值来自 `{"trailing_function_word", "terminal_punct", "trailing_content_word", "few_words"}`。
- 规则优先级：`trailing_function_word` 命中时，`terminal_punct` 不起作用（设计 §6.2）。词表与权重由实现者确定，但要满足下面测试中的阈值。

- [ ] **Step 1: 写失败的测试**

```python
@pytest.mark.parametrize("text", ["Please book it for", "Please book it for.", "I want to", "and the", "um"])
def test_trailing_function_word_is_confidently_incomplete(text):
    s = score(text); assert s.p_complete <= 0.3 and s.confidence >= 0.5 and "trailing_function_word" in s.rules
@pytest.mark.parametrize("text", ["Please book it for Wednesday afternoon.", "Stop. Make it Friday instead."])
def test_complete_sentence_scores_high(text):
    s = score(text); assert s.p_complete >= 0.85 and s.confidence >= 0.5
def test_unpunctuated_partial_of_complete_sentence_is_at_least_speculative():
    assert score("please book it for wednesday afternoon").p_complete >= 0.6
def test_few_words_lowers_confidence():
    assert score("Friday").confidence < 0.5 and "few_words" in score("Friday").rules
def test_empty_text_is_incomplete(): ...
```

- [ ] **Step 2～4: 运行失败 → 实现 → 运行通过**
- [ ] **Step 5: 提交**（`feat(endpointing): L1 确定性规则打分器`）

---

### Task 8: EndpointCascade（L1 + 动态阈值 + 硬上限）

**Files:**
- Create: `src/voice_runtime/endpointing/cascade.py`
- Test: `tests/test_cascade.py`

**Interfaces:**
- Consumes: `RuntimeConfig`、`TurnScorer`、`TurnScore`、`EventLog`、`SpeechEdge`
- Produces：
  - `def high_threshold(silence_ms: int, cfg: RuntimeConfig) -> float | None`：
    - `silence_ms < initial_silence_ms` 返回 `None`
    - 在 `initial_silence_ms` 到 `threshold_floor_at_ms` 之间，从 `high_start` 线性降到 `high_floor`
    - 之后保持 `high_floor`
  - `@dataclass(frozen=True) class EndpointDecision: reason: Literal["score_threshold","max_silence"]; level: int | None; p: float | None; threshold: float | None; decided_at_ms: int`
  - `class EndpointCascade`：
    - `__init__(self, cfg, log, scorers: list[TurnScorer], *, task_group_spawn: Callable[[Coroutine, str], asyncio.Task])`：`scorers[0]` 是 L1，L2、L3 可以没有
    - `on_speech_edge(self, edge: SpeechEdge) -> None`
    - `on_text(self, text: str, is_final: bool) -> None`：text_version 加 1，触发 L1 打分（仅在静音中）
    - `tick(self, now_ms: int) -> EndpointDecision | None`：由 Session 每 20ms 调用
    - `current_text: str`、`have_final_for_last_segment: bool`、`latest_l1: TurnScore | None`（预生成用）
    - `reset(self) -> None`：轮次结束时清空累计文本
    - `on_l1_scored: Callable[[TurnScore, str], None] | None`：每次 L1 打分完成都回调，供 Task 16 的预生成使用
  - 打分请求在 `(epoch, text_version)` 下发起；结果返回时只要其中一个变了，就写 `stale_event_dropped(gate="endpoint", phase="late", level=…)` 并丢弃。
  - 每次打分写 `turn_score`，payload：`{level, p, confidence, rules, latency_ms, epoch, text_version, decision: "commit_candidate"|"incomplete"|"uncertain"}`。
  - 打分器超时（超过本级预算）或抛异常都视为 uncertain，写 `provider_timeout` 或 `task_failed(level=…)`，**不向外抛出**。
- 本任务只实现 L1；`scorers[1:]` 的升级逻辑放在 Task 15。判停文本等待 final 最多 `final_wait_ms` 的逻辑由 Session 负责（Task 12），cascade 只给出 `EndpointDecision`。

- [ ] **Step 1: 写失败的测试**

```python
@pytest.mark.parametrize("t,exp", [(199, None), (200, 0.85), (600, 0.675), (1000, 0.5), (1400, 0.5)])
def test_high_threshold_curve(t, exp): assert high_threshold(t, RuntimeConfig()) == pytest.approx(exp) if exp else high_threshold(t, RuntimeConfig()) is None
def test_complete_text_commits_at_initial_silence(): ...      # 文本 "Please book it for Wednesday afternoon."，speech_end 在 t=1000 → tick 到 t=1200 时判停，reason=score_threshold，threshold=0.85
def test_incomplete_text_never_commits_before_max_silence():
    # 文本 "Please book it for"：t+700 时 tick 不判停；t+1500 时判停，reason=max_silence
def test_mid_score_commits_when_threshold_decays(): ...       # 脚本打分器给 p=0.6、conf=0.9 → 约在静音 771ms 的 tick（对齐到 20ms 后为 780）判停
def test_low_confidence_does_not_commit_even_if_p_high(): ... # p=0.9、conf=0.3 → 直到 max_silence 才判停
def test_speech_start_invalidates_inflight_score_as_stale(): ...  # L1 延迟 50ms，期间来了 speech_start → 写 stale_event_dropped(gate=endpoint)
def test_final_rewrites_text_and_old_score_is_stale():        # Review Focus 5
    # partial "book it for wednesday"，打分延迟 40ms；10ms 后 final "Book it for Wednesday afternoon." 到达
    # → 旧打分记为 stale；最终判停所依据的 turn_score 的 text_version 是 final 那一版
def test_scorer_exception_is_uncertain_not_fatal(): ...
```

- [ ] **Step 2～4: 运行失败 → 实现 → 运行通过**
- [ ] **Step 5: 提交**（`feat(endpointing): 动态阈值判停与 stale 打分丢弃`）

---

### Task 9: TruthLedger、Player 与 AudioSink

**Files:**
- Create: `src/voice_runtime/truth.py`、`src/voice_runtime/playback.py`
- Modify: `src/voice_runtime/queues.py`（新增 `get_nowait`）
- Test: `tests/test_playback.py`、`tests/test_queues.py`（追加 `get_nowait` 测试）

**Interfaces:**
- Consumes: `BoundedQueue`、`EventLog`、`AudioChunk`、`WordTiming`、`FRAME_SAMPLES`
- Produces（`truth.py`）：
  - `class TruthLedger`（每个 generation 一个）：
    - `__init__(self, generation_id: int)`
    - `on_generated(self, text: str) -> None`：累加完整生成文本
    - `on_synthesized(self, chunk_id: int, chunk: AudioChunk) -> None`
    - `on_enqueued(self, chunk_id: int, frames: int) -> None`
    - `on_played(self, chunk_id: int, frame_index: int) -> None`
    - `on_dropped(self, chunk_id: int, reason: str, gate: str) -> None`
    - `summary(self) -> dict`：返回 `{full_text, enqueued_text, heard_text, heard_range: [start, end], played_ms, chunks: [{chunk_id, text_range, samples, enqueued, played_samples, truncated, state}]}`
  - **听到的文本**：一个词的 chunk 内结束时刻 ≤ 该 chunk 已播放的时长，才算听到；按 chunk 顺序累加，遇到第一个没听完的词就停止。
- Produces（`playback.py`）：
  - `@dataclass(frozen=True) class PlaybackFrame: generation_id: int; chunk_id: int; frame_index: int; pcm: bytes`
  - `@dataclass(frozen=True) class PlaybackEOS: generation_id: int; truncated: bool`
  - `class GateState(StrEnum)`：`IDLE, HELD, PLAYING, PAUSED, CLOSED`
  - `class AudioSink(Protocol)`：`write(generation_id: int, frame_index: int, pcm: bytes) -> None`
  - `class FakeSink`：`writes: list[tuple[int, int, int]]`，元素是 (ts_ms, generation_id, frame_index)；`closed: bool`；关闭后再 `write` 会抛 `AssertionError`
  - `class Player`：
    - `__init__(self, cfg, log, queue: BoundedQueue[PlaybackFrame | PlaybackEOS], sink: AudioSink, is_active: Callable[[int], bool], ledgers: Callable[[int], TruthLedger], turn_of: Callable[[int], int])`
    - `state: GateState`；`current_generation: int | None`
    - `hold(self, generation_id: int) -> None`、`release(self, generation_id: int) -> None`
    - `pause(self) -> int`：返回暂停时的样本位置，并写 `playback_stopped(reason="paused_pending_decision", position_samples=…)`
    - `resume(self) -> None`：写 `playback_resumed(paused_ms=…)`
    - `stop_generation(self, generation_id: int, reason: str) -> None`：补记一次 progress，写 `playback_stopped(reason, playback_truth=ledger.summary())`，门控回到 IDLE
    - `close(self) -> None`：门控切到 CLOSED，sink 不再接受写入
    - `on_generation_done: Callable[[int, str], None]`：读到 EOS 时以 `"completed"` 或 `"truncated"` 回调
    - `async run(self) -> None`：每 `asyncio.sleep(0.02)` 一次 tick
  - tick 的规则：
    - 只有 PLAYING 时才 `get` 一帧。队列为空时**不阻塞**：用非阻塞读取（`BoundedQueue` 需要补一个 `get_nowait() -> T | None`，在本任务里加上并补测试），首次出现欠载时写 `playback_underrun`。
    - **闸③**：`is_active(gen)` 为假，或门控是 CLOSED → 写 `stale_event_dropped(gate="3", phase="late")`，不写 sink。
    - 某个 generation 的第一帧写入时，写 `playback_started`。
    - 每 `progress_interval_ms` 写一次 `playback_progress(ranges=[[gen, first_frame, last_frame], …], played_ms)`。

- [ ] **Step 1: 写失败的测试**
  - `test_pause_stops_output_within_one_tick_and_resume_continues_exact_position`：暂停后 sink 不再增长；resume 后接着写出的第一帧 frame_index 正好是暂停前最后一帧加 1（不重复、不漏帧）
  - `test_held_state_does_not_output_until_release`
  - `test_gate3_drops_frames_of_inactive_generation_and_never_writes`
  - `test_eos_triggers_completed_callback_and_stopped_event_with_truth`
  - `test_underrun_logs_once_and_does_not_emit_stopped`
  - `test_progress_ranges_cover_every_written_frame`：把所有 progress 区间展开，得到的帧集合等于 sink.writes 中的帧集合
  - `test_heard_text_excludes_partially_played_word`：
    ```python
    # chunk 词时长："Sure,"(100ms)、"booking"(120ms)；只播了 7 帧（140ms）
    # 期望 heard_text == "Sure,"、played_ms == 140；chunk 的 truncated 为 True
    ```
  - `test_closed_player_never_writes_sink`
- [ ] **Step 2～4: 运行失败 → 实现 → 运行通过**
- [ ] **Step 5: 提交**（`feat(playback): 播放器门控、闸③、EOS 与 Playback Truth`）

---

### Task 10: Generation 与 GenerationSupervisor

**Files:**
- Create: `src/voice_runtime/generation.py`
- Test: `tests/test_generation.py`

**Interfaces:**
- Consumes: `BoundedQueue`、`LLM`、`TTS`、`ProviderStream`、`TruthLedger`、`PlaybackFrame/PlaybackEOS`、`EventLog`、`RuntimeConfig`
- Produces：
  - `class GenState(StrEnum)`：`ACTIVE, CANCELLED, UPSTREAM_CLOSED, DONE`
  - `@dataclass class Generation`：`id: int; turn_id: int; user_text: str; speculative: bool; state: GenState; text_queue: BoundedQueue[str | None]; ledger: TruthLedger; cancelled_at_ms: int | None = None`。`None` 是 text 队列的 EOS。
  - `class GenerationSupervisor`：
    - `__init__(self, gen: Generation, cfg, log, llm: LLM, tts: TTS, messages: list[dict], playback_queue: BoundedQueue, is_active: Callable[[Generation], bool], on_failed: Callable[[Generation, str], None])`
    - `async run(self) -> None`：
      - 内部开一个 `TaskGroup`，包含 `gen-<id>/llm_bridge` 和 `gen-<id>/tts_bridge`
      - 用 `except*` 捕获非取消异常：写 `task_failed(task, error)`，调用 `on_failed(gen, "task_failed")`，**不往外抛**
    - `signal_cancel(self) -> None`：调用两个流的 `cancel()`，记录 `cancelled_at_ms`，并启动 grace 看门狗
    - `finish(self) -> None`：设置 `UPSTREAM_CLOSED`，然后 `signal_cancel()`，并向 playback 队列写入 `PlaybackEOS(truncated=True)`
  - 桥接规则：
    - **llm_bridge**：
      - 每次 `await anext(stream)` 都包在 `asyncio.timeout(...)` 里：第一项用 `llm_first_timeout_ms`，之后用 `inter_chunk_timeout_ms`。
      - 超时写 `provider_timeout(provider="llm", first=bool)`，然后调用 `on_failed(gen, "first_chunk_timeout")` 或 `finish()`（取决于是首包还是中途超时）。
      - 拿到数据后，写 `llm_chunk`、调用 `ledger.on_generated`，再执行 **闸①**：`text_queue.put(t, accept=lambda _: is_active(gen) and gen.state is ACTIVE)`。被拒时写 `stale_event_dropped(gate="1", phase="late" if gen.cancelled_at_ms else "rejected_on_wake")`。
      - 流结束后，往 text_queue 写入 `None`（EOS）。
    - **tts_bridge**：
      - 把 text_queue 包装成异步迭代器，交给 `tts.start`。
      - 超时计时方式同上，TTS 首包从第一段文本送入时开始计时。
      - 每个 chunk：写 `tts_chunk(chunk_id, text_range, samples, trimmed_samples)`；裁剪留到 Task 17 再插入；`ledger.on_synthesized`；然后切成 20ms 帧，逐帧执行**闸②**（`put`，`accept` 的条件同闸①）。
      - 一个 chunk 全部入队后，写一次 `audio_enqueued(chunk_id, frames, queue="playback", depth=…)`。
      - 被拒的 chunk 记一条 `stale_event_dropped(gate="2", phase=…, unit="chunk")`，同一个 chunk 只记一次。
      - 流结束后写入 `PlaybackEOS(truncated=False)`，同样要过闸②的校验。
      - cancel 之后收到的每个 chunk 都会被闸②拦下，`phase="late"`。
    - **grace 看门狗**：从 `signal_cancel` 起超过 `cancel_grace_ms` 后，如果 bridge 还没结束，就取消这两个 bridge，并写 `cancel_grace_exceeded`。

- [ ] **Step 1: 写失败的测试**（直接构造 supervisor，并配一个最简的 `is_active` 开关）
  - `test_happy_path_enqueues_frames_then_eos`
  - `test_backpressure_blocks_tts_at_capacity_without_timeout`：playback 队列容量 5 且没有消费者 → 3 秒内不写 `provider_timeout`；max_depth == 5
  - `test_cancel_drains_late_chunks_as_stale_late`：`late_after_cancel_ms=(50, 400)`，cancel 后 is_active 变为 False → 恰好写 2 条 `phase=late, gate="2"` 的事件；playback 队列里没有新的帧
  - `test_blocked_put_rejected_on_wake_is_not_counted_late`
  - `test_grace_exceeded_cancels_bridges_and_logs`：迟到项在 cancel 后 5000ms 才到，grace 为 1000 → 写 `cancel_grace_exceeded`，supervisor 结束
  - `test_first_chunk_timeout_calls_on_failed`
  - `test_mid_stream_timeout_finishes_with_truncated_eos_and_gate_finished`：finish 之后收到的迟到 chunk 被拦下，`gate="finished"`
  - `test_llm_exception_reports_task_failed_and_does_not_propagate`
- [ ] **Step 2～4: 运行失败 → 实现 → 运行通过**
- [ ] **Step 5: 提交**（`feat(generation): 三道闸中的闸①②、取消排空、超时与 finish`）

---

### Task 11: BargeInGate 判定逻辑

**Files:**
- Create: `src/voice_runtime/gate.py`
- Test: `tests/test_gate.py`

**Interfaces:**
- Consumes: `normalize_text`、`BackchannelScorer`、`RuntimeConfig`
- Produces：
  - 常量：`COMMAND_WORDS = ("stop", "wait", "no", "hold on")`、`BACKCHANNELS = ("uh-huh", "mm-hmm", "yeah", "yep", "right", "okay", "sure", "i see", "got it")`
  - `@dataclass class Utterance`：`utterance_id: int; start_ms: int; vad_ended_at_ms: int | None; partial_text: str; final_text: str | None; audio: bytearray`。`audio` 最多保留 `backchannel_audio_max_ms`，超出部分不再缓存。
  - `class GateResult(StrEnum)`：`PENDING, INTERRUPT, BACKCHANNEL, NOISE`
  - `async def decide(u: Utterance, now_ms: int, cfg: RuntimeConfig, scorer: BackchannelScorer) -> tuple[GateResult, str, dict]`，三项依次是结果、命中的规则名、细节
  - 规则名取值：`command_word`、`noise_empty_final`、`noise_timeout`、`backchannel`、`default_interrupt`、`pending`
  - 判定顺序严格按设计 §9.1：
    1. partial 里包含完整的指令词（按词边界匹配）→ 打断
    2. 噪声：final 为 `""`；或者 VAD 已结束、过了 `noise_timeout_ms` 仍没有 final
    3. 搭腔：final 非空，规范化后整句由附和词组成，评分取各切片的最大值 ≥ 0.5。评分超时（超过 `backchannel_budget_ms`）或失败时视为满足
    4. 默认打断：已有 final；或者 partial 中完整词数 ≥ 2，且这串词既不等于、也不是任何附和词组的前缀
    5. 都不满足时返回 `PENDING`
  - 切片：`audio` 按 80ms（2560 samples）切片，末尾不足一片的也算一片。

- [ ] **Step 1: 写失败的测试**

```python
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
])
def test_decision_table(...): ...
def test_backchannel_requires_max_slice_score_at_least_threshold(): ...  # 各切片分数 [0.1, 0.3] → 判为 default_interrupt；[0.1, 0.6] → 判为 backchannel
def test_backchannel_scorer_timeout_counts_as_backchannel(): ...
def test_utterance_audio_capped(): ...
def test_noise_timeout_inequality_holds_for_defaults():
    c = RuntimeConfig(); assert c.noise_timeout_ms > 150 + 20 + 20  # final_latency + 最大抖动 + 一帧 asr_in 排队延迟
```

- [ ] **Step 2～4: 运行失败 → 实现 → 运行通过**
- [ ] **Step 5: 提交**（`feat(gate): 打断门禁判定规则`）

---

### Task 12: Session 核心流程（LISTENING → 判停 → 生成 → 播放 → 轮次结束）

**Files:**
- Create: `src/voice_runtime/session.py`、`tests/helpers.py`
- Test: `tests/test_session_core.py`

**Interfaces:**
- Consumes：Task 2～11 的全部接口
- Produces：
  - `@dataclass class Providers`：`vad: VAD; asr: ASR; llm: LLM; tts: TTS; l1: TurnScorer; backchannel: BackchannelScorer; l2: TurnScorer | None = None; l3: TurnScorer | None = None`
  - `class CloseReason(StrEnum)`：`EXPLICIT_CLOSE = "explicit_close"`、`CLIENT_DISCONNECT = "client_disconnect"`、`SESSION_TASK_FAILED = "session_task_failed"`
  - `class SessionState(StrEnum)`：`LISTENING, PENDING, PLAYING, PAUSED, CLOSING, CLOSED`
  - `class Session`：
    - `__init__(self, cfg: RuntimeConfig, providers: Providers, source: ScriptedAudioSource, sink: AudioSink, log: EventLog)`
    - `async run(self) -> CloseReason`
    - `close(self, reason: CloseReason = CloseReason.EXPLICIT_CLOSE) -> None`：只设置 stop_event，可以重复调用
    - 属性：`state`、`queues: dict[str, BoundedQueue]`（键为 `"asr_in"`、`"playback"`）、`context: deque[dict]`（`maxlen=cfg.context_max_messages`）、`active_generation_id: int | None`
  - `tests/helpers.py`：
    - `def run_scenario(sc: Scenario, tmp_path: Path, **cfg_overrides) -> ScenarioResult`
      - `ScenarioResult` 的字段：`events: list[dict]`、`metrics: dict`、`sink: FakeSink`、`reason: CloseReason`
      - 用 `vloop.run` 运行；按 `sc.close_after` 的规定在指定事件之后自动调用 `close()`，`Scenario` 的定义见 Task 14
      - 在 Task 14 加入 `compute_metrics` 之前，`metrics` 先返回 `{}`
- 本任务覆盖设计 §8.1 和 §8.2 中**除打断门禁之外**的流程（门禁在 Task 13 接入）：
  - session 的 TaskGroup 包含四个任务：`session/audio_pump`、`session/asr_consumer`、`session/control`（20ms tick，驱动 cascade 和门禁）、`session/player`。
  - **audio_pump**：每一帧依次做这几件事：写 `audio_frame`（受 `log_audio_frames` 控制）、`vad.process`、`tracker.feed`，然后写入 `asr_in`（drop_oldest 模式，溢出时写 `queue_overflow(queue, depth, dropped)`）。
  - **speech_start 的处理**：如果在 LISTENING 状态且当前没有进行中的轮次，turn_id 加 1。
  - **asr_consumer**：写 `asr_partial` 或 `asr_final`，然后调用 `cascade.on_text`。
  - **判停**：
    - 拿到 `EndpointDecision` 后，如果最后一个语音段的 final 还没到，最多等 `final_wait_ms`。
    - 然后写 `endpoint_committed(decided_at, text_source, reason, level, p, threshold, text)`。
    - 接着启动 generation（generation_id 加 1），调用 `player.hold` 再立即 `release`，进入 PENDING 状态。
    - messages 由 context 加上本轮用户文本组成。
  - **状态切换**：`playback_started` 时进入 PLAYING；`on_generation_done` 时调用 `_end_turn(outcome)`。
  - **`_end_turn`**：
    - 写 `context_committed(role="user", text)`
    - 如果有听到的文本，再写 `context_committed(role="assistant", text=heard, interrupted)`
    - 写入 context，然后 `cascade.reset()`，状态回到 LISTENING
  - **generation 失败**：`on_failed` 依次执行：如果这个 generation 正在播放，就先调用 `player.stop_generation(gen, "generation_failed")`；然后 invalidate，写 `generation_failed(reason)`，再调用 `_end_turn("failed")`。
  - **invalidate**（设计 §10.1 的第 1、2 步，同步执行，中间不 await）：
    1. 置空 `active_generation_id`，状态设为 CANCELLED，写 `generation_cancelled(reason)`
    2. `playback.purge` 清掉旧 generation 的帧，写 `stale_event_dropped(gate="purge", phase="purge", frames=n)`
    3. 关闭旧 text 队列，然后调用 `supervisor.signal_cancel()`
    - 按 generation_id 做到幂等：重复调用时写 `generation_cancelled(duplicate=True)`，不做任何其他事
  - 本任务的关闭流程先用最简实现：设置 stop_event 后取消 TaskGroup 并写 `session_closed(reason)`。完整的关闭序列在 Task 14 实现。

- [ ] **Step 1: 写失败的测试**（直接构造脚本，不依赖 scenarios.py）
  - `test_complete_utterance_flows_to_playback_and_ends_turn`：
    - 事件顺序：`speech_end` → `endpoint_committed` → `llm_chunk` → `tts_chunk` → `audio_enqueued` → `playback_started` → `playback_stopped(completed)` → `context_committed`
    - 写入上下文的助手文本等于完整回复
  - `test_hesitation_does_not_commit_during_pause`：
    - 脚本为 Speech("Please book it for") + Silence(700) + Speech("Wednesday afternoon.")
    - 两段语音之间没有 `endpoint_committed`；全程只有 1 个 turn_id
  - `test_empty_llm_reply_ends_turn_without_playback`（Review Focus 2）：没有 `playback_started`；有一条 role=user 的 `context_committed`；状态回到 LISTENING
  - `test_asr_stall_overflows_asr_in_without_deadlock`（Review Focus 3）：
    - FakeASR 设置 `hang_at_ms=0`，用户连续说 3 秒话
    - 期望：`queue_overflow` 事件 ≥ 1；asr_in 的 max_depth == 50；session 能正常关闭
  - `test_first_chunk_timeout_fails_generation_and_returns_to_listening`
  - `test_context_is_bounded`：连续 25 轮之后 `len(session.context) == 20`
  - `test_turn_id_and_generation_id_come_from_data`：每个 `llm_chunk` 事件的 generation_id 都等于它所属 generation 的 id
- [ ] **Step 2～4: 运行失败 → 实现 → 运行通过**
- [ ] **Step 5: 提交**（`feat(session): 核心轮次流程、invalidate 与上下文提交`）

---

### Task 13: Session 接入打断门禁

**Files:**
- Modify: `src/voice_runtime/session.py`
- Test: `tests/test_session_bargein.py`

**Interfaces:**
- Consumes: `decide`、`Utterance`、`GateResult`（Task 11）；`Player.pause/resume/stop_generation`（Task 9）
- Produces：Session 新增以下行为（对外接口不变）：
  - **暂停**：在 PLAYING 或 PENDING 状态下，`tracker.speech_frames_run` 达到 `pause_min_speech_frames` 时：
    - 新建一个 `Utterance`（`utterance_id` 加 1）
    - PLAYING 时调用 `player.pause()` 并进入 PAUSED；PENDING 时门控保持 held
    - 这段话的 ASR 结果按 `segment_start_ms`～`segment_end_ms` 与 utterance 的时间重叠来归属（设计 §9.1）
  - **判定**：control tick 每次都调用 `decide`，结果不是 PENDING 时写 `bargein_decision(result, rule, utterance_id, words, scores, late_interrupt)`。
  - **INTERRUPT**：
    - 先 `stop_generation(gen, "interrupted")`，再 invalidate（reason=`"barge_in"`，PENDING 状态下为 `"user_continued"`）
    - 调用 `_end_turn("interrupted")`；PENDING 状态下不结束轮次（设计 §8.1）
    - turn_id 加 1（PENDING 时不加）
    - 用这段话已有的文本作为新一轮累计文本的开头，并把这段话的 `speech_end` 交给 cascade
  - **BACKCHANNEL / NOISE**：丢弃这段转写，调用 `player.resume()`，回到 PLAYING（PENDING 时保持 PENDING）
  - **迟到的词**：已经判为 NOISE 的 utterance 又收到 ASR 结果时，重新执行 `decide`；结果是 INTERRUPT 就写 `late_interrupt=True` 并按打断处理，否则丢弃。最近 4 个已判定的 utterance 用 `deque(maxlen=4)` 保存。
  - 门禁运行期间，speech_start 不让 turn_id 加 1。
  - **endpoint 和预生成的边界**：只有 LISTENING 状态才把 speech 边界交给 cascade。

- [ ] **Step 1: 写失败的测试**
  - `test_command_word_interrupt_stops_within_250ms_and_starts_new_turn`：
    - 机器人回复约 4 秒；用户在 `AfterEvent("playback_started", 1200)` 时说 "Stop. Make it Friday instead."
    - 从 script_marker 的开口时刻到第一条 `playback_stopped` ≤ 250ms
    - 之后依次有 `generation_cancelled(reason=barge_in)` 和 `stale_event_dropped(gate=purge, frames>0)`
    - 旧轮次的 assistant `context_committed` 带 `interrupted=True`，文本是完整回复的真前缀
    - 新轮次的 user 文本是 "Stop. Make it Friday instead."，并且出现了新 generation 的 `playback_started`
  - `test_noise_80ms_pauses_then_resumes_and_completes`：
    - 依次出现 `bargein_decision(result=noise, rule=noise_empty_final)` 和 `playback_resumed`
    - 最终 `playback_stopped(completed)`
    - sink 中该 generation 的 frame_index 连续不重复
  - `test_backchannel_resumes_and_transcript_not_in_context`："Yeah." → 判为 backchannel；之后所有 `context_committed` 中都不出现 "yeah"
  - `test_i_see_not_interrupted_at_first_word`
  - `test_late_words_after_noise_trigger_late_interrupt`：ASR 对这段话卡住超过 noise_timeout 之后才吐出 "Stop" → 写 `late_interrupt=True`，回复被打断
  - `test_user_continues_during_pending_merges_into_same_turn`：判停后、首帧播出前用户又说 "afternoon." → 写 `generation_cancelled(reason=user_continued)`；turn_id 不变；下一次判停的文本包含两段
  - `test_cough_during_pending_keeps_generation`：PENDING 阶段出现 80ms 噪声 → generation 保留，最终正常播放
  - `test_double_barge_in_handles_both`（Review Focus 1）：
    - 第二次打断发生在新回复 `playback_started` 后 300ms
    - 两次打断的停播延迟都 ≤ 250ms
    - sink 中不出现已取消 generation 在取消之后写入的帧
- [ ] **Step 2～4: 运行失败 → 实现 → 运行通过**
- [ ] **Step 5: 提交**（`feat(session): 接入打断门禁（暂停/判定/恢复/打断/迟到词）`）

---

### Task 14: 关闭序列、指标与场景 A'～E

**Files:**
- Modify: `src/voice_runtime/session.py`
- Create: `src/voice_runtime/metrics.py`、`src/voice_runtime/scenarios.py`
- Test: `tests/test_lifecycle.py`、`tests/test_metrics.py`、`tests/test_scenarios.py`、`tests/test_constraints.py`

**Interfaces:**
- Produces（关闭，设计 §13）：
  - 把 Task 12 的最简关闭替换为完整的关闭序列：
    1. stop_event 触发后进入 CLOSING
    2. invalidate 所有 generation
    3. 如果有正在播放的 generation，先调用 `player.stop_generation(gen, "session_closed")`，再 `player.close()`
    4. 取消 `session/*` 这几个循环任务
    5. 等待 TaskGroup 退出。超过 `close_grace_ms` 时写 `close_timeout(round, tasks)`，并对剩余任务再 `cancel()` 一次，按轮次重复
    6. 关闭各个队列，记录残留数量
    7. 写 `session_closed(reason, residual, queue_max_depth, overdue_task_count, error?)`
  - `run()` 的结构：TaskGroup 外层用 `except*` 处理 session 级异常，走异常路径；关闭序列放在 `finally` 里，用 `asyncio.shield` 包住；`session_closed` 只写一次。
  - 断开连接：`audio_pump` 发现 `source.disconnected` 时调用 `close(CLIENT_DISCONNECT)`，然后退出循环。
- Produces（指标）：`def compute_metrics(events: list[dict]) -> dict`，键名固定为：
  - `endpoint_latency_ms: list[int]`
  - `turn_end_to_first_audio_ms: list[int]`
  - `speech_start_detection_ms: list[int]`、`playback_stop_ms: list[int]`、`interruption_decision_ms: list[int]`、`overlap_ms: list[int]`
  - `false_interruption_count`、`false_pause_count`、`pause_total_ms`
  - `stale_chunk_received`、`stale_chunk_played`、`stale_purged_frames`、`stale_rejected_on_wake`
  - `queue_max_depth: dict[str, int]`
  - 各项的计算口径严格按设计 §9.2 和 §11.4
- Produces（场景）：
  - `@dataclass class Scenario`：`name: str; segments: list; llm_replies: dict[str, str]; default_reply: str; llm_faults: StreamFaults; tts_faults: StreamFaults; asr_kwargs: dict; cfg_overrides: dict; close_after: tuple[str, int, int]`
  - `close_after` 的含义是：第 n 个某类事件出现后，再过若干毫秒关闭。
  - 场景构造函数：`scenario_a_prime()`、`scenario_b()`、`scenario_c()`、`scenario_d()`、`scenario_e()`，以及 Task 16 要用的 `scenario_a()`
  - 场景 C 和 E 的机器人回复要足够长，按 `word_duration_ms` 计算总时长约 4 秒，测试里要断言 3600～4400ms。

- [ ] **Step 1: 写失败的测试**
  - `tests/test_lifecycle.py`：
    - `test_no_running_tasks_after_close`：session 结束后，`asyncio.all_tasks()` 中名字以 `session/` 或 `gen-` 开头的任务数为 0（在 loop 内部断言）
    - `test_swallowing_provider_triggers_recancel_rounds`：`swallow_first_cancel=True` 且 LLM 处于 `hang_after` 状态时关闭 → `close_timeout` 至少 1 轮；最终写出 `session_closed`，`overdue_task_count ≥ 1`
    - `test_external_cancel_of_run_still_writes_session_closed`
    - `test_session_task_exception_closes_with_reason`：VAD 抛异常 → `reason=session_task_failed`，`error` 字段为 `"RuntimeError: boom"`
    - `test_disconnect_closes_with_client_disconnect`
    - `test_close_twice_and_close_while_paused_is_idempotent`（Review Focus 4）：只有 1 条 `session_closed`；sink 中没有任何写入的时间戳晚于 `session_closed`
    - `test_concurrent_invalidate_is_idempotent`：同一 tick 内对同一个 generation 调用 3 次 invalidate → 只有 1 条非 duplicate 的 `generation_cancelled`
  - `tests/test_metrics.py`：用手工构造的事件列表逐项验证每个指标的计算口径，包括 purge、rejected_on_wake 不计入 `stale_chunk_received`
  - `tests/test_scenarios.py`（设计 §12 的场景断言，界值都从配置推算）：
    ```python
    def test_A_prime_no_extra_queueing(tmp_path):
        r = run_scenario(scenario_a_prime(), tmp_path)
        assert r.metrics["turn_end_to_first_audio_ms"][0] <= 300 + 20 + 200 + 20 + 40
    def test_B_no_early_playback_and_timely_endpoint(tmp_path):  # 停顿期间 0 条 endpoint_committed / playback_started；endpoint latency ≤ 200 + 300 + 50
    def test_C_interrupt_under_250ms(tmp_path):                  # playback_stop_ms[0] ≤ 250；被打断 generation 的上下文只含听到的部分
    def test_D_noise_not_permanent(tmp_path):                    # false_interruption_count == 0，false_pause_count == 1，回复最终 completed
    def test_E_late_chunks_never_played(tmp_path):
        r = run_scenario(scenario_e(), tmp_path)                 # E 场景：tts late_after_cancel_ms=(50, 3000)，cancel_grace_ms=4000，close_grace_ms=5000
        cancel_ts = ...; new_first = ...                         # 从日志里取
        assert new_first - cancel_ts < 3000 < 4000                # 设计 §12 的时序不等式
        assert r.metrics["stale_chunk_received"] == 2 and r.metrics["stale_chunk_played"] == 0
    def test_E_late_chunk_after_close_never_written(tmp_path): ...
    def test_all_queues_bounded(tmp_path):                       # 每个 queue_max_depth 都 ≤ 对应容量；所有 BoundedQueue 的 capacity 都 > 0
    def test_trace_is_byte_identical_across_runs(tmp_path):      # 场景 C 跑两次，两份 jsonl 的字节完全一致
    ```
  - `tests/test_constraints.py`：`test_no_wall_clock_or_threads_in_src`，用 grep 扫描 `src/voice_runtime`（不含 `adapters/`），断言不出现 Global Constraints 里列出的禁用符号
- [ ] **Step 2～4: 运行失败 → 实现 → 运行通过**（Run: `.venv/bin/pytest -v`，Expected: 全部通过）
- [ ] **Step 5: 提交**（`feat(session): 完整关闭序列、指标计算与场景 A'～E 测试`）

---

## P1

### Task 15: L2/L3 提前升级

**Files:**
- Modify: `src/voice_runtime/endpointing/cascade.py`
- Test: `tests/test_cascade.py`（追加）

**Interfaces:**
- Consumes: `ScriptedTurnScorer`（Task 5）
- Produces：`EndpointCascade` 支持 `scorers[1]`（L2）和 `scorers[2]`（L3）：
  - 某一级结果为 uncertain 时，立即发起下一级
  - 在同一个 `(epoch, text_version, level)` 下，最多只有一个请求在途
  - 结果为 incomplete（`p ≤ low` 且 `confidence ≥ min_conf`）时不再升级
  - "当前分数"取已返回的最高一级的结果
  - L2 的输入音频取自一个 3 秒的环形缓冲。Session 每帧调用 `cascade.feed_audio(pcm)`，缓冲区用 `deque(maxlen=150)` 存帧

- [ ] **Step 1: 写失败的测试**
  - `test_uncertain_l1_escalates_immediately_at_speech_end`：L1 给 0.6/0.4 → L2 请求在 speech_end 的同一 tick 发起，不等 200ms
  - `test_l2_result_overrides_l1_for_decision`：L2 给 0.95/0.9 → 在静音 200ms 时判停，`level=2`
  - `test_at_most_one_inflight_per_level`：200ms 内文本不变 → L2 只被调用 1 次
  - `test_incomplete_does_not_escalate`
  - `test_l3_timeout_falls_back_to_threshold_curve`
  - `test_audio_ring_is_bounded`：喂入 10 秒音频后缓冲区只有 150 帧
- [ ] **Step 2～4: 运行失败 → 实现 → 运行通过**
- [ ] **Step 5: 提交**（`feat(endpointing): L2/L3 提前升级`）

### Task 16: 投机预生成与场景 A

**Files:**
- Modify: `src/voice_runtime/session.py`
- Test: `tests/test_speculation.py`、`tests/test_scenarios.py`（追加 A）

**Interfaces:**
- Consumes: `cascade.on_l1_scored`、`normalize_text`、`Player.hold/release`、invalidate
- Produces：Session 的预生成行为（`cfg.speculation_enabled` 打开时生效）：
  - **启动**：在静音中，L1 的 p ≥ `speculate_threshold`，并且规范化后的文本和当前预生成依据的文本不同时：
    - 先 invalidate 旧的预生成（`reason="speculation_superseded"`）
    - 再启动新的 generation（`speculative=True`），调用 `player.hold(gen)`，写 `speculation_started(text)`
  - **判停时比较**：
    - 文本相同：写 `speculation_adopted`，调用 `player.release`，进入 PENDING
    - 文本不同：invalidate 预生成（`reason="speculation_discarded"`），写 `speculation_discarded(spec_text, final_text)`，然后按 Task 12 的流程新起一个 generation
  - 预生成中被拦下的已入队帧，计入 `ledger.on_dropped(reason="speculation_discarded")`
  - speech_start 不作废预生成，判停时再比较文本即可

- [ ] **Step 1: 写失败的测试**
  - `test_A_speculation_adopted_and_faster_than_A_prime`：
    - 出现 `speculation_adopted`
    - `playback_started` 的 sequence_number 大于 `endpoint_committed`
    - A 的 `turn_end_to_first_audio_ms` 小于 A' 的同一指标
  - `test_speculation_discarded_when_text_changes`：判停前又来了一个改变文本的 final → 写 `speculation_discarded`；sink 中没有被作废 generation 的帧
  - `test_speculation_held_audio_never_exceeds_playback_capacity`：playback 的 max_depth ≤ 75
  - `test_speculation_not_restarted_for_same_text`
- [ ] **Step 2～4: 运行失败 → 实现 → 运行通过**
- [ ] **Step 5: 提交**（`feat(session): 投机预生成与场景 A`）

### Task 17: TTS 开头底噪裁剪

**Files:**
- Create: `src/voice_runtime/trim.py`
- Modify: `src/voice_runtime/generation.py`（在 tts_bridge 里对每个 generation 的第一个 chunk 调用）
- Test: `tests/test_trim.py`

**Interfaces:**
- Produces：`def trim_leading_noise(chunk: AudioChunk, *, max_trim_ms: int, threshold_rms: float = 1000.0) -> AudioChunk`
  - 按 20ms 窗口从头扫描，跳过 rms 低于阈值的窗口，最多裁掉 `max_trim_ms`
  - 返回新的 chunk：`trimmed_samples` 等于裁掉的样本数，各词时间整体前移裁掉的时长（结果不小于 0）
  - 第一个窗口就超过阈值时，原样返回 chunk
  - 设计里说"裁剪方法留到测试时决定"，这里选用能量阈值法，并在 DESIGN.md 中说明

- [ ] **Step 1: 写失败的测试**
  - `test_trims_leading_noise_and_shifts_word_timings`：120ms 底噪加语音 → `trimmed_samples == 1920`，第一个词的 `start_ms == 0`
  - `test_never_trims_speech`
  - `test_trim_capped_at_max`
  - `test_first_audio_excludes_leading_noise_in_session`：`leading_noise_ms=120` 时，开启裁剪比关闭裁剪的 `turn_end_to_first_audio_ms` 少 120ms 左右（±20ms）；`tts_chunk.trimmed_samples == 1920`
- [ ] **Step 2～4: 运行失败 → 实现 → 运行通过**
- [ ] **Step 5: 提交**（`feat(trim): TTS 开头底噪裁剪与时间戳平移`）

---

## P2

### Task 18: 真实模型适配器（可选依赖）

**Files:**
- Create: `src/voice_runtime/adapters/__init__.py`、`adapters/silero_vad.py`、`adapters/firered_turn.py`、`adapters/smart_turn.py`、`adapters/jev_turn.py`
- Modify: `pyproject.toml`（填写 `real` extras：`onnxruntime`、`numpy`、`httpx`，以及 Silero 需要的包，版本由实现者选当前稳定版并固定）
- Test: `tests/test_adapters.py`

**Interfaces:**
- Produces：每个适配器都实现 Task 5 中对应的 Protocol，构造函数接收模型路径或 API key：
  - `SileroVAD(model_path: Path)`：内部把 20ms 帧拼成 512 samples 再推理
  - `FireRedTurnScorer(model_path: Path)`
  - `SmartTurnScorer(model_path: Path)`
  - `JevTurnScorer(api_key: str, endpoint: str)`
- 适配器只要求能被实例化，并且通过协议返回结果。**不进入核心测试，也不在虚拟 loop 中运行**。允许使用线程，`test_constraints` 的扫描排除了 `adapters/`。

- [ ] **Step 1: 写测试**：`pytest.importorskip("onnxruntime")`；各适配器类满足对应的 Protocol（用 `isinstance` 检查 `runtime_checkable` 的 Protocol）；模型文件不存在时构造函数抛 `FileNotFoundError`
- [ ] **Step 2: 实现并运行**：`.venv/bin/pytest tests/test_adapters.py -v`。没装 extras 时 Expected 为 skipped
- [ ] **Step 3: 提交**（`feat(adapters): 真实模型适配器接口（可选依赖）`）

---

## 交付物

### Task 19: sample-output 与文档

**Files:**
- Create: `scripts/run_scenarios.py`、`sample-output/`（生成）、`README.md`、`DESIGN.md`
- Test: `tests/test_run_scenarios.py`

**Interfaces:**
- Consumes: `scenarios.py`、`compute_metrics`、`Session`
- Produces：`python scripts/run_scenarios.py --out sample-output` 依次跑 A'、A、B、C、D、E 六个场景，生成：
  - `sample-output/<name>.trace.jsonl`
  - `sample-output/barge_in.trace.jsonl`（场景 C 的完整 trace）
  - `sample-output/metrics.json`
  - `sample-output/metrics.md`（各场景指标汇总表）

- [ ] **Step 1: 写测试** `test_run_scenarios_writes_all_outputs(tmp_path)`：运行后上述文件都存在；`metrics.json` 中每个场景都包含 Task 14 规定的全部键
- [ ] **Step 2: 实现脚本，运行测试并生成** `sample-output/`
- [ ] **Step 3: 在 `sample-output/analysis.md` 写简短分析**：只依据 `barge_in.trace.jsonl` 解释这次打断，引用具体的 `sequence_number`，说明开口、暂停、判定、作废、purge、迟到 chunk 被拦下、新一轮开始这几步
- [ ] **Step 4: 写 README.md**，包含：
  - 环境（Python 3.12 + venv）
  - 运行和测试命令
  - 架构概览
  - 实际耗时（由用户填写）
  - 已完成和未完成的部分（P2 只接了接口；回声识别只写了方案）
  - AI 工具使用说明：骨架写好，**哪些设计是用户自己做的、是否完全理解代码，由用户本人补充**
- [ ] **Step 5: 写 DESIGN.md（2～4 页）**：由设计文档压缩而成，必须包含：
  - 架构图、关键状态与事件、并发与取消模型、Playback Truth、Endpointing 策略、取舍与已知局限
  - 需求文档 §6.3 的 7 个问答
  - B～E 的补充说明，每项 3～5 句，D 要写出 §9.2 中保护机制带来的延迟代价数字
  - RuleTurnScorer 的词表和权重、裁剪方法，以及最终的默认参数
  - 回声识别的方案（设计 §9.4，只写方案不实现），放在"已知局限"里
- [ ] **Step 6: 运行全部测试**：`.venv/bin/pytest -v`，Expected 为全部通过（adapters 显示 skipped）
- [ ] **Step 7: 提交**（`docs: README、DESIGN、sample-output 与分析`）

> `agent-chat.md` 由用户自己导出对话记录并去除 PII，这一项不在本计划的自动化范围内。
