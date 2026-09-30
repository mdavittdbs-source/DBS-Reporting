from types import SimpleNamespace as NS

import pytest

from dbs_reporting import usage


def test_cost_per_model():
    # Sonnet 5.5: $2 in, $10 out, $0.20 cache read, cache write 1.25x input.
    assert usage.cost("claude-sonnet-5-5", 1_000_000, 100_000, 0, 0) == pytest.approx(3.0)
    assert usage.cost("claude-sonnet-5-5", 0, 0, 1_000_000, 1_000_000) == pytest.approx(0.2 + 2.5)
    assert usage.cost("claude-opus-5-5", 10_000, 1_000, 0, 0) == pytest.approx(0.06)
    assert usage.cost("claude-unknown", 1, 1, 0, 0) is None


def test_add_message_uses_per_attempt_breakdown():
    totals = usage.empty()
    plain = NS(usage=NS(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0,
                        cache_creation_input_tokens=0, iterations=None))
    usage.add_message(totals, plain, "claude-sonnet-5-5")
    # A refusal fallback: the first attempt ran on Opus, the rest on Sonnet; each priced at its own rate.
    fallback = NS(usage=NS(iterations=[
        NS(model="claude-opus-5-5", input_tokens=1000, output_tokens=0, cache_read_input_tokens=0,
           cache_creation_input_tokens=0),
        NS(model="claude-sonnet-5-5", input_tokens=1000, output_tokens=100, cache_read_input_tokens=500,
           cache_creation_input_tokens=0),
    ]))
    usage.add_message(totals, fallback, "claude-opus-5-5")
    assert totals["requests"] == 2
    assert totals["input_tokens"] == 3000 and totals["output_tokens"] == 300 and totals["cache_read_tokens"] == 500
    expected = (1000 * 2 + 200 * 10) / 1e6 + (1000 * 4) / 1e6 + (1000 * 2 + 100 * 10 + 500 * 0.2) / 1e6
    assert totals["cost_usd"] == pytest.approx(expected) and totals["priced"]


def test_unknown_model_marks_unpriced():
    totals = usage.empty()
    usage.add_message(totals, NS(usage=NS(input_tokens=5, output_tokens=5, cache_read_input_tokens=0,
                                          cache_creation_input_tokens=0, iterations=None)), "claude-new")
    assert totals["priced"] is False and totals["cost_usd"] == 0
    assert usage.combine([totals, usage.empty()])["priced"] is False
