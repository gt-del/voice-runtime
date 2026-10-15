"""测试工具：薄封装 voice_runtime.runner，trace 写到 tmp_path/<name>.jsonl。"""
from pathlib import Path

from voice_runtime import runner
from voice_runtime.runner import ScenarioResult, build_session  # noqa: F401  测试从这里导入
from voice_runtime.scenarios import Scenario


def run_scenario(sc: Scenario, tmp_path: Path, **kwargs) -> ScenarioResult:
    return runner.run_scenario(sc, tmp_path / f"{sc.name}.jsonl", **kwargs)
