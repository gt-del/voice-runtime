"""L1 确定性规则打分器：只看文本本身的语言信号。"""
from voice_runtime.providers.base import TurnScore
from voice_runtime.providers.fake import normalize_text

# 句末出现这些词，几乎可以断定话没说完：介词、连词、冠词、限定词、助动词、填充词。
FUNCTION_WORDS = frozenset(
    "for to of in on at with by from into about "
    "and or but so if because that which "
    "the a an my your his her its our their this these those "
    "is are was were be am will would can could do does did has have had "
    "um uh er hmm".split()
)
TERMINAL_PUNCT = (".", "?", "!", "。", "？", "！")
FEW_WORDS_MAX = 2

# (p_complete, confidence)
W_FUNCTION = (0.1, 0.9)
W_PUNCT = (0.95, 0.8)
W_CONTENT = (0.7, 0.55)
FEW_WORDS_CONF_CAP = 0.3


class RuleTurnScorer:
    async def score(self, text: str, audio: bytes | None = None) -> TurnScore:
        words = normalize_text(text).split()
        if not words:
            return TurnScore(0.0, 0.6)
        if words[-1] in FUNCTION_WORDS:
            # 优先级最高：即使带句末标点（"Please book it for."）也判没说完。
            return TurnScore(*W_FUNCTION, rules=("trailing_function_word",))
        if text.rstrip().endswith(TERMINAL_PUNCT):
            p, conf = W_PUNCT
            rules = ["terminal_punct"]
        else:
            p, conf = W_CONTENT
            rules = ["trailing_content_word"]
        if len(words) <= FEW_WORDS_MAX:
            conf = min(conf, FEW_WORDS_CONF_CAP)
            rules.append("few_words")
        return TurnScore(p, conf, tuple(rules))
