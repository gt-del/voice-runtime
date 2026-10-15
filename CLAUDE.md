# CLAUDE.md

面试展示用 demo：单会话最小 Voice Runtime（Audio In → VAD/Endpointing → ASR → LLM → TTS → Playback）。
完整需求见 [docs/requirements.md](docs/requirements.md)。

## 开发要求

1. **Think Before Coding** — *Don't assume. Don't hide confusion. Surface tradeoffs.*
   - State assumptions explicitly; if uncertain, ask rather than guess.
   - Present competing interpretations instead of silently picking one.
   - Call out inconsistencies, confusion, and tradeoffs as you find them.

2. **Simplicity First** — *Minimum code that solves the problem. Nothing speculative.*
   - No features beyond what was asked.
   - No abstractions for single-use code.
   - No "flexibility" or configurability that wasn't requested.
   - No error handling for scenarios that cannot occur.

3. **Surgical Changes** — *Touch only what you must. Clean up only your own mess.*
   - Match the surrounding style and conventions.
   - Don't refactor unbroken adjacent code that's orthogonal to the task.
   - Only remove what your changes made unused; leave pre-existing dead code alone unless asked.

4. **简体中文输出** — 所有回复与文档均使用简体中文。
