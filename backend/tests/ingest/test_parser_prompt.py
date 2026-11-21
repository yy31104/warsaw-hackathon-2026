from alerting import llm
from alerting.assist.parser_suggest import sample_prompt


def test_model_sample_spans_the_batch_and_shares_byte_budget_among_lines():
    lines = ["first café " * 200, "account name", "middle token=not-a-real-key",
             "unselected line", "last λ " * 900]
    prompt = sample_prompt(lines)
    first, middle, last = prompt.splitlines()
    assert first.startswith("first café")
    assert middle == "middle token=<redacted>"
    assert last.startswith("last λ")
    assert len(prompt.encode("utf-8")) <= llm.MAX_INPUT_CHARS


def test_short_batch_sends_each_sample_once():
    assert sample_prompt(["one", "two"]) == "one\ntwo"
