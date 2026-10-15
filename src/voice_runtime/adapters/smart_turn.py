"""Smart Turn（音频端到端 turn 模型，ONNX）：Whisper 特征 -> 完成概率。"""
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
        np = self._np
        wav = np.frombuffer(audio, dtype="<i2").astype(np.float32) / 32768.0
        wav = wav[-MAX_SECONDS * SAMPLE_RATE :]
        feats = self._fe(wav, sampling_rate=SAMPLE_RATE, return_tensors="np").input_features
        p = float(self._sess.run(None, {"input_features": feats.astype(np.float32)})[0].reshape(-1)[0])
        return TurnScore(p_complete=p, confidence=abs(p - 0.5) * 2)
