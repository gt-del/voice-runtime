"""Jev 远程 turn 评分（HTTP API）。构造时不发网络请求。

【未验证】本适配器的输入/输出形状、张量名与输出语义均为未经验证的占位假设，
不保证能在真实服务上运行；接入时须对照其实际 I/O 修改。

未核实项（均为假定）：请求体字段、鉴权头、响应字段 p_complete/confidence 及其语义。
假定的接口：POST {endpoint}，JSON {"text": ...}，返回 {"p_complete": float, "confidence": float}。
"""
from voice_runtime.providers.base import TurnScore


class JevTurnScorer:
    def __init__(self, api_key: str, endpoint: str) -> None:
        self._api_key = api_key
        self._endpoint = endpoint

    async def score(self, text: str, audio: bytes | None = None) -> TurnScore:
        import httpx

        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.post(
                self._endpoint,
                json={"text": text},
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            r.raise_for_status()
            d = r.json()
        return TurnScore(p_complete=float(d["p_complete"]), confidence=float(d["confidence"]))
