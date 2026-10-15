"""L1 确定性规则打分器：只看文本本身的语言信号。"""
from voice_runtime.providers.base import TurnScore
from voice_runtime.providers.fake import normalize_text

# 无歧义的"没说完"词：介词、连词、冠词、不能收尾的限定词、填充词。压过句末标点。
STRONG_FUNCTION_WORDS = frozenset(
    "for to of in on at with by from into about "
    "and or but if because which "
    "the a an my your our their "
    "um uh er hmm".split()
)
# 有歧义的词：可能收尾（"I think so." "I called her."），也可能没说完。
# 带句末标点时按标点规则走；不带标点时给低置信度的中间分，让级联升级。
AMBIGUOUS_FUNCTION_WORDS = frozenset(
    "so that this these those her his its "
    "is are was were be am will would can could do does did has have had".split()
)
TERMINAL_PUNCT = (".", "?", "!")  # 仅英文；省略号不算
FEW_WORDS_MAX = 2

# (p_complete, confidence)
W_FUNCTION = (0.1, 0.9)
W_PUNCT = (0.95, 0.8)
W_CONTENT = (0.7, 0.55)
W_AMBIGUOUS = (0.4, 0.4)
FEW_WORDS_CONF_CAP = 0.3


class RuleTurnScorer:
    async def score(self, text: str, audio: bytes | None = None) -> TurnScore:
        text = text.replace("…", "...")
        words = normalize_text(text).split()
        if not words:
            return TurnScore(0.0, 0.6)
        if words[-1] in STRONG_FUNCTION_WORDS:
            # 优先级最高：即使带句末标点（"Please book it for."）也判没说完。
            return TurnScore(*W_FUNCTION, rules=("trailing_function_word",))
        stripped = text.rstrip()
        punct = stripped.endswith(TERMINAL_PUNCT) and not stripped.endswith("..")
        if words[-1] in AMBIGUOUS_FUNCTION_WORDS and not punct:
            return TurnScore(*W_AMBIGUOUS, rules=("trailing_function_word",))
        if punct:
            p, conf = W_PUNCT
            rules = ["terminal_punct"]
        else:
            p, conf = W_CONTENT
            rules = ["trailing_content_word"]
        if len(words) <= FEW_WORDS_MAX:
            conf = min(conf, FEW_WORDS_CONF_CAP)
            rules.append("few_words")
        return TurnScore(p, conf, tuple(rules))
