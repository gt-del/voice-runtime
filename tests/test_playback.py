import asyncio

import pytest

from voice_runtime.audio import FRAME_SAMPLES, AudioChunk, WordTiming
from voice_runtime.config import RuntimeConfig
from voice_runtime.events import (PLAYBACK_PROGRESS, PLAYBACK_RESUMED, PLAYBACK_STARTED,
                                  PLAYBACK_STOPPED, PLAYBACK_UNDERRUN, STALE_EVENT_DROPPED,
                                  EventLog)
from voice_runtime.playback import (FakeSink, GateState, PlaybackEOS, PlaybackFrame, Player)
from voice_runtime.queues import BoundedQueue
from voice_runtime.truth import TruthLedger
from voice_runtime.vloop import run

PCM = bytes(FRAME_SAMPLES * 2)


class Rig:
    def __init__(self, tmp_path, active=(1,)):
        self.events: list[dict] = []
        self.log = EventLog(tmp_path / "e.jsonl", "s")
        self.log.add_listener(self.events.append)
        self.queue = BoundedQueue("playback", 75)
        self.sink = FakeSink()
        self.active = set(active)
        self.ledgers: dict[int, TruthLedger] = {}
        self.done: list[tuple[int, str]] = []
        self.next_chunk: dict[int, int] = {}
        self.player = Player(RuntimeConfig(), self.log, self.queue, self.sink,
                             is_active=lambda g: g in self.active,
                             ledgers=self.ledger, turn_of=lambda g: g + 100)
        self.player.on_generation_done = lambda g, r: self.done.append((g, r))

    def ledger(self, gen: int) -> TruthLedger:
        return self.ledgers.setdefault(gen, TruthLedger(gen))

    async def frames(self, gen: int, start: int, n: int, chunk_id: int | None = None):
        if chunk_id is None:  # 未指定 chunk 时，按真实流水线先在账本登记一个 n 帧的 chunk
            chunk_id = self.next_chunk.get(gen, 0)
            self.next_chunk[gen] = chunk_id + 1
            self.ledger(gen).on_synthesized(chunk_id, AudioChunk(PCM * n, (0, 0)))
            self.ledger(gen).on_enqueued(chunk_id, n)
        for i in range(start, start + n):
            await self.queue.put(PlaybackFrame(gen, chunk_id, i, PCM))

    def of(self, event_type: str) -> list[dict]:
        return [e for e in self.events if e["event_type"] == event_type]

    def start(self):
        return asyncio.create_task(self.player.run(), name="session/player")


def test_pause_stops_output_within_one_tick_and_resume_continues_exact_position(tmp_path):
    async def main():
        r = Rig(tmp_path)
        await r.frames(1, 0, 30)
        r.player.hold(1)
        r.player.release(1)
        r.start()
        await asyncio.sleep(0.11)
        pos = r.player.pause()
        n = len(r.sink.writes)
        await asyncio.sleep(0.2)
        assert len(r.sink.writes) == n
        assert r.player.state is GateState.PAUSED
        r.player.resume()
        await asyncio.sleep(0.05)
        return r, pos, n

    r, pos, n = run(main())
    idx = [w[2] for w in r.sink.writes]
    assert n == 5 and pos == 5 * FRAME_SAMPLES
    assert idx == list(range(len(idx))) and len(idx) > n
    assert idx[n] == idx[n - 1] + 1
    stopped = r.of(PLAYBACK_STOPPED)
    assert [(e["payload"]["reason"], e["payload"]["position_samples"]) for e in stopped] == [
        ("paused_pending_decision", 5 * FRAME_SAMPLES)]
    assert [e["payload"]["paused_ms"] for e in r.of(PLAYBACK_RESUMED)] == [200]
    assert all(e["generation_id"] == 1 and e["turn_id"] == 101 for e in stopped + r.of(PLAYBACK_RESUMED))


def test_held_state_does_not_output_until_release(tmp_path):
    async def main():
        r = Rig(tmp_path)
        await r.frames(1, 0, 10)
        r.player.hold(1)
        r.start()
        await asyncio.sleep(0.21)  # 错开 tick 时刻，避免同一虚拟时刻的先后歧义
        held_writes = len(r.sink.writes)
        r.player.release(2)  # 不是当前 generation，不放行
        await asyncio.sleep(0.1)
        wrong_release_writes = len(r.sink.writes)
        r.player.release(1)
        await asyncio.sleep(0.04)
        return r, held_writes, wrong_release_writes

    r, held, wrong = run(main())
    assert held == 0 and wrong == 0
    assert len(r.sink.writes) == 2
    assert r.player.state is GateState.PLAYING
    started = r.of(PLAYBACK_STARTED)
    assert len(started) == 1 and started[0]["generation_id"] == 1 and started[0]["turn_id"] == 101


def test_gate3_drops_frames_of_inactive_generation_and_never_writes(tmp_path):
    async def main():
        r = Rig(tmp_path, active=(2,))
        await r.frames(1, 0, 3)
        await r.queue.put(PlaybackEOS(1, truncated=False))
        await r.frames(2, 0, 2)
        r.player.hold(2)
        r.player.release(2)
        r.start()
        await asyncio.sleep(0.2)
        return r

    r = run(main())
    assert [w[1:] for w in r.sink.writes] == [(2, 0), (2, 1)]
    drops = r.of(STALE_EVENT_DROPPED)
    assert [(e["generation_id"], e["payload"]["gate"], e["payload"]["phase"], e["payload"]["unit"])
            for e in drops] == [(1, "3", "late", "frame")] * 3 + [(1, "3", "late", "eos")]
    assert [e["payload"]["frame_index"] for e in drops[:3]] == [0, 1, 2]
    assert r.done == [] and r.of(PLAYBACK_STOPPED) == []


def test_eos_triggers_completed_callback_and_stopped_event_with_truth(tmp_path):
    async def main():
        r = Rig(tmp_path, active=(1, 2))
        led = r.ledger(1)
        led.on_generated("Hi.")
        chunk = AudioChunk(PCM * 3, (0, 3), [WordTiming("Hi.", 0, 60)])
        led.on_synthesized(0, chunk)
        led.on_enqueued(0, 3)
        await r.frames(1, 0, 3, chunk_id=0)
        await r.queue.put(PlaybackEOS(1, truncated=False))
        r.player.hold(1)
        r.player.release(1)
        r.start()
        await asyncio.sleep(0.2)
        state_after_1 = (r.player.state, r.player.current_generation)
        await r.frames(2, 0, 1)
        await r.queue.put(PlaybackEOS(2, truncated=True))
        r.player.hold(2)
        r.player.release(2)
        await asyncio.sleep(0.1)
        return r, state_after_1

    r, state_after_1 = run(main())
    assert state_after_1 == (GateState.IDLE, None)
    assert r.done == [(1, "completed"), (2, "truncated")]
    stopped = r.of(PLAYBACK_STOPPED)
    assert [(e["generation_id"], e["turn_id"], e["payload"]["reason"]) for e in stopped] == [
        (1, 101, "completed"), (2, 102, "truncated")]
    truth = stopped[0]["payload"]["playback_truth"]
    assert truth["full_text"] == "Hi." and truth["heard_text"] == "Hi."
    assert truth["heard_range"] == [0, 3] and truth["played_ms"] == 60
    assert truth["chunks"][0]["truncated"] is False


def test_underrun_logs_once_and_does_not_emit_stopped(tmp_path):
    async def main():
        r = Rig(tmp_path)
        await r.frames(1, 0, 2)
        r.player.hold(1)
        r.player.release(1)
        r.start()
        await asyncio.sleep(0.3)
        first = len(r.of(PLAYBACK_UNDERRUN))
        await r.frames(1, 2, 2)
        await asyncio.sleep(0.3)
        return r, first

    r, first = run(main())
    assert first == 1
    assert len(r.of(PLAYBACK_UNDERRUN)) == 2  # 写出新帧后重新武装
    assert r.of(PLAYBACK_STOPPED) == []
    assert r.player.state is GateState.PLAYING
    assert [w[2] for w in r.sink.writes] == [0, 1, 2, 3]


def test_progress_ranges_cover_every_written_frame(tmp_path):
    async def main():
        r = Rig(tmp_path)
        await r.frames(1, 0, 23)
        await r.queue.put(PlaybackEOS(1, truncated=False))
        r.player.hold(1)
        r.player.release(1)
        r.start()
        await asyncio.sleep(0.13)
        r.player.pause()
        await asyncio.sleep(0.05)
        r.player.resume()
        await asyncio.sleep(0.5)
        return r

    r = run(main())
    covered = []
    for e in r.of(PLAYBACK_PROGRESS):
        for gen, a, b in e["payload"]["ranges"]:
            covered += [(gen, i) for i in range(a, b + 1)]
    written = [(w[1], w[2]) for w in r.sink.writes]
    assert len(written) == 23
    assert sorted(covered) == written  # 不重复、不遗漏
    ts = [e["timestamp"] for e in r.of(PLAYBACK_PROGRESS)]
    assert ts[:2] == [120, 130]  # 120ms 时满 100ms 补记；130ms 暂停时 flush 剩余
    last = r.of(PLAYBACK_PROGRESS)[-1]["payload"]
    assert last["played_ms"] == 23 * 20


def test_heard_text_excludes_partially_played_word():
    led = TruthLedger(1)
    led.on_generated("Sure, booking now.")
    samples = 220 * 16
    led.on_synthesized(0, AudioChunk(bytes(samples * 2), (0, 13),
                                     [WordTiming("Sure,", 0, 100), WordTiming("booking", 100, 220)]))
    led.on_enqueued(0, 11)
    for i in range(7):
        led.on_played(0, i, i * 20)
    s = led.summary()
    assert s["heard_text"] == "Sure,"
    assert s["heard_range"] == [0, 5]
    assert s["played_ms"] == 140
    assert s["full_text"] == "Sure, booking now."
    assert s["enqueued_text"] == "Sure, booking"
    c = s["chunks"][0]
    assert c["truncated"] is True and c["played_samples"] == 7 * FRAME_SAMPLES
    assert c["text_range"] == [0, 13] and c["samples"] == samples and c["enqueued"] is True


def test_closed_player_never_writes_sink(tmp_path):
    async def main():
        r = Rig(tmp_path)
        await r.frames(1, 0, 20)
        r.player.hold(1)
        r.player.release(1)
        task = r.start()
        await asyncio.sleep(0.05)
        r.player.close()
        n = len(r.sink.writes)
        r.player.hold(1)
        r.player.release(1)
        await asyncio.sleep(0.2)
        return r, n, task.done()

    r, n, run_done = run(main())
    assert n == 2 and len(r.sink.writes) == n
    assert r.player.state is GateState.CLOSED and r.sink.closed and run_done
    with pytest.raises(AssertionError):
        r.sink.write(1, 99, PCM)


def test_progress_emitted_by_time_even_during_underrun(tmp_path):
    async def main():
        r = Rig(tmp_path)
        await r.frames(1, 0, 3)
        r.player.hold(1)
        r.player.release(1)
        r.start()
        await asyncio.sleep(0.15)
        return r

    r = run(main())
    first_write = r.sink.writes[0][0]
    progress = r.of(PLAYBACK_PROGRESS)
    assert [e["payload"]["ranges"] for e in progress] == [[[1, 0, 2]]]
    assert progress[0]["payload"]["played_ms"] == 60
    assert progress[0]["timestamp"] - first_write <= 100 + 20


def test_gate3_drops_consecutive_stale_items_within_one_tick(tmp_path):
    async def main():
        r = Rig(tmp_path, active=(2,))
        await r.frames(1, 0, 10)
        await r.queue.put(PlaybackEOS(1, truncated=False))
        await r.frames(2, 0, 2)
        r.player.hold(2)
        r.player.release(2)
        r.start()
        await asyncio.sleep(0.03)  # 只经过一个 tick（20ms）
        return r

    r = run(main())
    assert r.sink.writes == [(20, 2, 0)]
    assert len(r.of(STALE_EVENT_DROPPED)) == 11


def test_no_underrun_before_first_frame_written(tmp_path):
    async def main():
        r = Rig(tmp_path)
        r.player.hold(1)
        r.player.release(1)
        r.start()
        await asyncio.sleep(0.11)
        before = len(r.of(PLAYBACK_UNDERRUN))
        await r.frames(1, 0, 1)
        await asyncio.sleep(0.1)
        return r, before

    r, before = run(main())
    assert before == 0
    kinds = [e["event_type"] for e in r.events if e["event_type"] in (PLAYBACK_STARTED, PLAYBACK_UNDERRUN)]
    assert kinds == [PLAYBACK_STARTED, PLAYBACK_UNDERRUN]


def _sure_booking_ledger(played_frames: int) -> TruthLedger:
    led = TruthLedger(1)
    led.on_generated("Sure, booking now.")
    led.on_synthesized(0, AudioChunk(bytes(220 * 32), (0, 13),
                                     [WordTiming("Sure,", 0, 100), WordTiming("booking", 100, 220)]))
    led.on_enqueued(0, 11)
    for i in range(played_frames):
        led.on_played(0, i, i * 20)
    return led


def test_heard_text_word_ending_exactly_at_played_position_is_heard():
    s = _sure_booking_ledger(5).summary()  # 100ms，正好是 "Sure," 的 end_ms
    assert (s["heard_text"], s["heard_range"]) == ("Sure,", [0, 5])
    s = _sure_booking_ledger(11).summary()  # 220ms，正好是 "booking" 的 end_ms
    assert (s["heard_text"], s["heard_range"]) == ("Sure, booking", [0, 13])
    assert s["chunks"][0]["truncated"] is False


def test_heard_scan_stops_at_word_missing_from_text():
    led = TruthLedger(1)
    led.on_generated("Sure, booking")
    led.on_synthesized(0, AudioChunk(bytes(220 * 32), (0, 13),
                                     [WordTiming("Sure,", 0, 100), WordTiming("cooking", 100, 220)]))
    led.on_enqueued(0, 11)
    for i in range(11):
        led.on_played(0, i, i * 20)
    s = led.summary()
    assert (s["heard_text"], s["heard_range"]) == ("Sure,", [0, 5])


def test_enqueued_text_keeps_whitespace_between_chunks():
    led = TruthLedger(1)
    led.on_generated("Sure, booking now.")
    led.on_synthesized(0, AudioChunk(bytes(100 * 32), (0, 5), [WordTiming("Sure,", 0, 100)]))
    led.on_synthesized(1, AudioChunk(bytes(120 * 32), (6, 13), [WordTiming("booking", 0, 120)]))
    led.on_synthesized(2, AudioChunk(bytes(80 * 32), (14, 18), [WordTiming("now.", 0, 80)]))
    led.on_enqueued(0, 5)
    led.on_enqueued(1, 6)
    assert led.summary()["enqueued_text"] == "Sure, booking"


def test_progress_cadence_is_exactly_progress_interval_while_playing(tmp_path):
    async def main():
        r = Rig(tmp_path)
        await r.frames(1, 0, 30)
        r.player.hold(1)
        r.player.release(1)
        r.start()
        await asyncio.sleep(0.43)
        return r

    r = run(main())
    assert [(e["timestamp"], e["payload"]["ranges"]) for e in r.of(PLAYBACK_PROGRESS)] == [
        (120, [[1, 0, 4]]), (220, [[1, 5, 9]]), (320, [[1, 10, 14]]), (420, [[1, 15, 19]])]


def test_drop_unplayed_keeps_partially_played_chunk_played_and_truncated():
    led = _sure_booking_ledger(7)  # chunk 0 播了 7/11 帧
    led.on_synthesized(1, AudioChunk(bytes(80 * 32), (14, 18), [WordTiming("now.", 0, 80)]))
    led.on_enqueued(1, 4)
    led.drop_unplayed("barge_in", "purge")
    c0, c1 = led.summary()["chunks"]
    assert (c0["state"], c0["truncated"], c0["played_samples"]) == ("played", True, 7 * FRAME_SAMPLES)
    assert "reason" not in c0 and "gate" not in c0
    assert (c1["state"], c1["reason"], c1["gate"], c1["played_samples"]) == \
        ("dropped", "barge_in", "purge", 0)


def test_on_dropped_never_overrides_played_frames():
    led = _sure_booking_ledger(3)
    led.on_dropped(0, "late", "2")
    assert led.summary()["chunks"][0]["state"] == "played"


def test_played_words_since_uses_write_time_of_each_word():
    led = TruthLedger(1)
    led.on_generated("Sure, booking now.")
    led.on_synthesized(0, AudioChunk(bytes(260 * 32), (0, 18), [
        WordTiming("Sure,", 40, 140), WordTiming("booking", 140, 260)]))  # 开头 40ms 底噪
    led.on_enqueued(0, 13)
    for i in range(9):  # 0..8 帧：底噪 2 帧，"Sure," 5 帧（写于 40..120），"booking" 2 帧（140、160）
        led.on_played(0, i, 1000 + i * 20)
    assert led.played_words_since(0) == ["Sure,", "booking"]
    assert led.played_words_since(1000 + 120) == ["Sure,", "booking"]  # "Sure," 最后一帧写于 1120
    assert led.played_words_since(1000 + 140) == ["booking"]
    assert led.played_words_since(1000 + 180) == []
