"""FireRedChat 文本 turn detector（ONNX）。

接入点示意：输入 token ids，输出“话已说完”的概率。真实模型的分词器需按其发布
说明替换 `_encode`；此处用 UTF-8 字节作占位编码，仅展示输入整形。
"""
from pathlib import Path

from voice_runtime.providers.base import TurnScore


class FireRedTurnScorer:
    def __init__(self, model_path: Path) -> None:
        if not Path(model_path).is_file():
            raise FileNotFoundError(model_path)
        self._load(Path(model_path))

    def _load(self, model_path: Path) -> None:
        import numpy as np
        import onnxruntime as ort

        self._np = np
        self._sess = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        self._input = self._sess.get_inputs()[0].name

    def _encode(self, text: str):
        return self._np.array([list(text.encode("utf-8"))], dtype=self._np.int64)

    async def score(self, text: str, audio: bytes | None = None) -> TurnScore:
        logits = self._sess.run(None, {self._input: self._encode(text)})[0]
        p = float(logits.reshape(-1)[-1])
        return TurnScore(p_complete=p, confidence=abs(p - 0.5) * 2)
