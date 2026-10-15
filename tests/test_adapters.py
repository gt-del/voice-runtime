"""适配器测试：不需要重依赖（惰性加载被替换），真实推理部分缺依赖时跳过。"""
import pytest

from voice_runtime.adapters.firered_turn import FireRedTurnScorer
from voice_runtime.adapters.jev_turn import JevTurnScorer
from voice_runtime.adapters.silero_vad import SileroVAD
from voice_runtime.adapters.smart_turn import SmartTurnScorer
from voice_runtime.providers.base import VAD, TurnScorer

FILE_ADAPTERS = [(SileroVAD, VAD), (FireRedTurnScorer, TurnScorer), (SmartTurnScorer, TurnScorer)]


@pytest.mark.parametrize("cls,_proto", FILE_ADAPTERS)
def test_missing_model_file_raises(cls, _proto, tmp_path):
    with pytest.raises(FileNotFoundError):
        cls(tmp_path / "nope.onnx")


@pytest.mark.parametrize("cls,proto", FILE_ADAPTERS)
def test_satisfies_protocol_without_heavy_deps(cls, proto, tmp_path, monkeypatch):
    model = tmp_path / "m.onnx"
    model.write_bytes(b"")
    monkeypatch.setattr(cls, "_load", lambda self, path: None)
    assert isinstance(cls(model), proto)


def test_jev_satisfies_protocol_and_no_network_at_construction():
    assert isinstance(JevTurnScorer("key", "http://127.0.0.1:1/x"), TurnScorer)


def test_silero_buffers_until_first_window(tmp_path, monkeypatch):
    from voice_runtime.audio import Frame

    model = tmp_path / "m.onnx"
    model.write_bytes(b"")
    monkeypatch.setattr(SileroVAD, "_load", lambda self, path: None)
    monkeypatch.setattr(SileroVAD, "_infer", lambda self, raw: 0.9)
    vad = SileroVAD(model)
    pcm = bytes(640)  # 320 采样
    assert vad.process(Frame(0, 0, pcm)) == 0.0  # 320 < 512
    assert vad.process(Frame(1, 20, pcm)) == 0.9  # 640 >= 512


def test_silero_real_model_requires_deps(tmp_path):
    pytest.importorskip("onnxruntime")
    pytest.importorskip("numpy")
    bad = tmp_path / "bad.onnx"
    bad.write_bytes(b"not a model")
    with pytest.raises(Exception):
        SileroVAD(bad)
