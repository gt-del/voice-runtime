import json
import runpy
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "run_scenarios.py"
NAMES = ["A_prime", "A", "B", "C", "D", "E"]
METRIC_KEYS = {
    "endpoint_latency_ms", "turn_end_to_first_audio_ms", "speech_start_detection_ms",
    "playback_stop_ms", "interruption_decision_ms", "overlap_ms", "false_interruption_count",
    "false_pause_count", "pause_total_ms", "stale_chunk_received", "stale_chunk_played",
    "stale_purged_frames", "stale_rejected_on_wake", "queue_max_depth",
}


def _run(out: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["run_scenarios.py", "--out", str(out)])
    runpy.run_path(str(SCRIPT), run_name="__main__")


def test_run_scenarios_writes_all_outputs(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    for name in NAMES:
        assert (tmp_path / f"{name}.trace.jsonl").stat().st_size > 0
    assert (tmp_path / "barge_in.trace.jsonl").read_bytes() == (tmp_path / "C.trace.jsonl").read_bytes()
    assert (tmp_path / "metrics.md").read_text(encoding="utf-8").startswith("# ")
    metrics = json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))
    assert set(metrics) == set(NAMES)
    for name in NAMES:
        assert set(metrics[name]) == METRIC_KEYS


def test_rerun_is_identical(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    first = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    _run(tmp_path, monkeypatch)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == first
