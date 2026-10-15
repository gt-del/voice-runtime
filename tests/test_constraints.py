import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "voice_runtime"
FORBIDDEN = re.compile(r"time\.time|time\.monotonic|datetime\.now|to_thread|run_in_executor"
                       r"|threading")


def test_no_wall_clock_or_threads_in_src():
    files = [p for p in SRC.rglob("*.py") if "adapters" not in p.relative_to(SRC).parts]
    assert files
    hits = [f"{p.relative_to(SRC)}:{i}: {line.strip()}"
            for p in files for i, line in enumerate(p.read_text().splitlines(), 1)
            if FORBIDDEN.search(line)]
    assert hits == []
