"""文本规范化：小写、去标点、合并空白（判停、门禁、预生成比对与假 Provider 共用）。"""
import string

PUNCT = str.maketrans("", "", string.punctuation)


def normalize_text(s: str) -> str:
    return " ".join(s.lower().translate(PUNCT).split())
