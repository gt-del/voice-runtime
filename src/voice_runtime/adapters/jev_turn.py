"""Jev 远程 turn 评分（HTTP API）。构造时不发网络请求。

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
