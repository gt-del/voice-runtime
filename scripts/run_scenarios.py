"""依次跑 A'、A、B、C、D、E 六个场景，输出 trace 与指标汇总。

用法：python scripts/run_scenarios.py --out sample-output
"""
import argparse
import json
import shutil
from pathlib import Path

from voice_runtime.runner import run_scenario
from voice_runtime.scenarios import (scenario_a, scenario_a_prime, scenario_b, scenario_c,
                                     scenario_d, scenario_e)

SCENARIOS = [scenario_a_prime, scenario_a, scenario_b, scenario_c, scenario_d, scenario_e]
# metrics.md 汇总表的列：列表型指标显示为逗号分隔的值
COLUMNS = [
    "endpoint_latency_ms", "turn_end_to_first_audio_ms", "speech_start_detection_ms",
    "playback_stop_ms", "interruption_decision_ms", "overlap_ms", "false_interruption_count",
    "false_pause_count", "pause_total_ms", "stale_chunk_received", "stale_chunk_played",
    "stale_purged_frames", "stale_rejected_on_wake",
]


def _cell(v) -> str:
    if isinstance(v, list):
        return ", ".join(map(str, v)) if v else "-"
    return str(v)


def _markdown(all_metrics: dict[str, dict]) -> str:
    names = list(all_metrics)
    lines = ["# 各场景指标汇总", "", "单位：ms（计数类除外）；列表表示该场景内每次发生的值，`-` 表示未发生。", "",
             "| 指标 | " + " | ".join(names) + " |",
             "|---|" + "---|" * len(names)]
    for col in COLUMNS:
        lines.append(f"| {col} | " + " | ".join(_cell(all_metrics[n][col]) for n in names) + " |")
    lines.append("| queue_max_depth | " + " | ".join(
        "<br>".join(f"{k}={v}" for k, v in sorted(all_metrics[n]["queue_max_depth"].items()))
        for n in names) + " |")
    return "\n".join(lines) + "\n"


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    all_metrics: dict[str, dict] = {}
    for make in SCENARIOS:
        sc = make()
        trace = out / f"{sc.name}.trace.jsonl"
        trace.unlink(missing_ok=True)  # EventLog 是追加写，先删旧文件保证重跑结果一致
        all_metrics[sc.name] = run_scenario(sc, trace).metrics
        if sc.name == "C":
            shutil.copyfile(trace, out / "barge_in.trace.jsonl")
    (out / "metrics.json").write_text(
        json.dumps(all_metrics, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "metrics.md").write_text(_markdown(all_metrics), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("sample-output"))
    main(parser.parse_args().out)
