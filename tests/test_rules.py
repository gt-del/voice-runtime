import asyncio

import pytest

from voice_runtime.endpointing.rules import RuleTurnScorer


def score(text):
    return asyncio.run(RuleTurnScorer().score(text))


@pytest.mark.parametrize("text", ["Please book it for", "Please book it for.", "I want to", "and the", "um"])
def test_trailing_function_word_is_confidently_incomplete(text):
    s = score(text)
    assert s.p_complete <= 0.3 and s.confidence >= 0.5 and "trailing_function_word" in s.rules


def test_function_word_beats_terminal_punct():
    assert "terminal_punct" not in score("Please book it for.").rules


@pytest.mark.parametrize("text", ["Please book it for Wednesday afternoon.", "Stop. Make it Friday instead."])
def test_complete_sentence_scores_high(text):
    s = score(text)
    assert s.p_complete >= 0.85 and s.confidence >= 0.5 and "terminal_punct" in s.rules


def test_unpunctuated_partial_of_complete_sentence_is_at_least_speculative():
    s = score("please book it for wednesday afternoon")
    assert s.p_complete >= 0.6 and "trailing_content_word" in s.rules


def test_few_words_lowers_confidence():
    assert score("Friday").confidence < 0.5 and "few_words" in score("Friday").rules


def test_empty_text_is_incomplete():
    s = score("   ")
    assert s.p_complete <= 0.3 and s.rules == ()
