"""Smart Turn（音频端到端 turn 模型，ONNX）：Whisper 特征 -> 完成概率。

【未验证】本适配器的输入/输出形状、张量名与输出语义均为未经验证的占位假设，
不保证能在真实模型上运行；接入时须对照其实际 I/O 修改。
未核实项：输入名 input_features、WhisperFeatureExtractor(chunk_length=8)、标量概率输出。
音频假定为 16kHz 单声道 PCM16 小端（与 Frame 一致）。
"""
from pathlib import Path

from voice_runtime.audio import SAMPLE_RATE
from voice_runtime.providers.base import TurnScore

MAX_SECONDS = 8  # 模型只看最近 8 秒音频


class SmartTurnScorer:
    def __init__(self, model_path: Path) -> None:
        if not Path(model_path).is_file():
            raise FileNotFoundError(model_path)
        self._load(Path(model_path))

    def _load(self, model_path: Path) -> None:
        import numpy as np
        import onnxruntime as ort
        from transformers import WhisperFeatureExtractor

        self._np = np
        self._sess = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        self._fe = WhisperFeatureExtractor(chunk_length=MAX_SECONDS)

    async def score(self, text: str, audio: bytes | None = None) -> TurnScore:
        if not audio:
            return TurnScore(p_complete=0.5, confidence=0.0)  # 无音频：不表态
        if len(audio) % 2:
            audio = audio[:-1]  # PCM16 需偶数字节
        np = self._np
        wav = np.frombuffer(audio, dtype="<i2").astype(np.float32) / 32768.0
        wav = wav[-MAX_SECONDS * SAMPLE_RATE :]
        feats = self._fe(wav, sampling_rate=SAMPLE_RATE, return_tensors="np").input_features
        p = float(self._sess.run(None, {"input_features": feats.astype(np.float32)})[0].reshape(-1)[0])
        return TurnScore(p_complete=p, confidence=abs(p - 0.5) * 2)
