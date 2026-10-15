from voice_runtime.audio import Frame
from voice_runtime.config import RuntimeConfig
from voice_runtime.speech import SpeechTracker

S, N = 0.9, 0.05


def _run(tracker, probs, start_seq=0):
    edges = []
    for i, p in enumerate(probs):
        seq = start_seq + i
        e = tracker.feed(Frame(seq, seq * 20, b""), p)
        if e:
            edges.append((i, e))
    return edges


def test_three_speech_frames_trigger_start_two_do_not():
    t = SpeechTracker(RuntimeConfig())
    assert _run(t, [S, S]) == []
    assert not t.in_speech
    edges = _run(SpeechTracker(RuntimeConfig()), [S, S, S])
    assert [(i, e.kind) for i, e in edges] == [(2, "start")]


def test_start_ts_is_first_speech_frame():
    t = SpeechTracker(RuntimeConfig())
    edges = _run(t, [N, N, S, S, S])
    assert edges[0][1].ts_ms == 40
    assert t.in_speech


def test_end_on_fourth_silence_backdated_to_first_silence():
    t = SpeechTracker(RuntimeConfig())
    edges = _run(t, [S, S, S, S, N, N, N, N])
    assert [(i, e.kind, e.ts_ms) for i, e in edges] == [(2, "start", 0), (7, "end", 80)]
    assert t.last_speech_end_ms == 80
    assert not t.in_speech


def test_80ms_noise_triggers_start_then_end():
    t = SpeechTracker(RuntimeConfig())
    edges = _run(t, [S, S, S, S] + [N] * 4)
    assert [e.kind for _, e in edges] == ["start", "end"]


def test_epoch_increments_each_start():
    t = SpeechTracker(RuntimeConfig())
    assert t.epoch == 0
    edges = _run(t, [S] * 3 + [N] * 4 + [S] * 3)
    assert [e.epoch for _, e in edges if e.kind == "start"] == [1, 2]
    assert t.epoch == 2


def test_speech_frames_run_resets_on_non_speech():
    t = SpeechTracker(RuntimeConfig())
    _run(t, [S, S])
    assert t.speech_frames_run == 2
    _run(t, [N], start_seq=2)
    assert t.speech_frames_run == 0


def test_short_silence_inside_speech_does_not_end():
    t = SpeechTracker(RuntimeConfig())
    edges = _run(t, [S, S, S, N, N, N, S, N, N, N])
    assert [e.kind for _, e in edges] == ["start"]
