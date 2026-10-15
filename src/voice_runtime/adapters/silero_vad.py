"""Silero VAD（ONNX）：把 20ms 帧攒成 512 采样窗口再推理。

【未验证】本适配器的输入/输出形状、张量名与输出语义均为未经验证的占位假设，
不保证能在真实模型上运行；接入时须对照其实际 I/O 修改。
假设输入名 input/state/sr、输出为 (prob, state)、64 采样 context（v5 风格）均未核实。
"""
from pathlib import Path

from voice_runtime.audio import SAMPLE_RATE, Frame

WINDOW = 512  # 16kHz 下 Silero 要求的窗口大小
CONTEXT = 64  # v5 模型需要在窗口前拼接上一窗口末尾 64 个采样


class SileroVAD:
    def __init__(self, model_path: Path) -> None:
        if not Path(model_path).is_file():
            raise FileNotFoundError(model_path)
        self._load(Path(model_path))
        self._buf = bytearray()
        self._prob = 0.0

    def _load(self, model_path: Path) -> None:
        import numpy as np
        import onnxruntime as ort

        self._np = np
        self._sess = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._ctx = np.zeros((1, CONTEXT), dtype=np.float32)

    def process(self, frame: Frame) -> float:
        """返回最新概率；第一个窗口攒满前为 0.0。"""
        self._buf += frame.pcm
        while len(self._buf) >= WINDOW * 2:
            raw = bytes(self._buf[: WINDOW * 2])
            del self._buf[: WINDOW * 2]
            self._prob = self._infer(raw)
        return self._prob

    def _infer(self, raw: bytes) -> float:
        np = self._np
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32)[None] / 32768.0
        inp = np.concatenate([self._ctx, x], axis=1)
        out, self._state = self._sess.run(
            None, {"input": inp, "state": self._state, "sr": np.array(SAMPLE_RATE, dtype=np.int64)}
        )
        self._ctx = x[:, -CONTEXT:]
        return float(out.reshape(-1)[0])
