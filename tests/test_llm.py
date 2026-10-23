"""LLM wrapper: retries with feedback, no resend on 413, pacing, deadline, token ceiling (plan groups 4-5)."""

import pytest
from pydantic import BaseModel

from app.config import Settings
from app.llm import (
    BudgetExhausted,
    Completion,
    ContextTooLarge,
    FakeLLM,
    LLMCallFailed,
    LLMClient,
    RateLimited,
    RequestBudget,
    TokenPacer,
)
from fakes import FakeClock

MESSAGES = [{"role": "system", "content": "You write titles."}, {"role": "user", "content": "Title please"}]
TRUNCATED = Completion(content='{"ti', finish_reason="length", total_tokens=2000)


class Title(BaseModel):
    title: str


def _client(replies: list[object], settings: Settings, clock: FakeClock) -> tuple[LLMClient, FakeLLM]:
    fake = FakeLLM(replies)
    pacer = TokenPacer(settings.pacer_window_tokens, settings.pacer_window_s, clock)
    return LLMClient(fake, pacer, settings, clock=clock, sleep=clock.sleep), fake


def _budget(settings: Settings, clock: FakeClock, deadline_s: float | None = None) -> RequestBudget:
    return RequestBudget(settings.request_token_ceiling, deadline_s or settings.request_deadline_s, clock)


def _ask(client: LLMClient, budget: RequestBudget, max_completion_tokens: int = 2000) -> Title:
    return client.chat_json(
        MESSAGES, Title, purpose="test", max_completion_tokens=max_completion_tokens, budget=budget
    )


def test_bad_json_is_retried_with_feedback_and_counted_in_the_ledger(settings, clock) -> None:
    client, fake = _client(["not json", {"title": "ok"}], settings, clock)
    budget = _budget(settings, clock)

    assert _ask(client, budget).title == "ok"
    assert [call.outcome for call in budget.calls] == ["invalid_output", "ok"]
    assert "rejected" in fake.requests[1][-1]["content"]


def test_truncated_output_is_a_failed_attempt(settings, clock) -> None:
    client, _ = _client([TRUNCATED] * 3, settings, clock)
    budget = _budget(settings, clock)

    with pytest.raises(LLMCallFailed, match="truncated"):
        _ask(client, budget)
    assert [call.outcome for call in budget.calls] == ["truncated"] * 3


def test_context_too_large_from_the_provider_is_never_resent(settings, clock) -> None:
    client, fake = _client([ContextTooLarge("too large"), {"title": "never"}], settings, clock)
    budget = _budget(settings, clock)

    with pytest.raises(ContextTooLarge):
        _ask(client, budget)
    assert len(fake.requests) == 1
    assert budget.tokens_used == 0


def test_over_cap_call_is_not_sent_and_nothing_is_trimmed(settings, clock) -> None:
    client, fake = _client([{"title": "x"}], settings, clock)

    with pytest.raises(ContextTooLarge, match="context too large"):
        _ask(client, _budget(settings, clock), max_completion_tokens=settings.per_call_token_cap)
    assert fake.requests == []


def test_rate_limit_waits_for_retry_after_without_using_an_attempt(settings, clock) -> None:
    client, _ = _client([RateLimited(2.5), {"title": "ok"}], settings, clock)
    budget = _budget(settings, clock)

    assert _ask(client, budget).title == "ok"
    assert clock.sleeps == [2.5]
    assert [call.outcome for call in budget.calls] == ["rate_limited", "ok"]


def test_pacer_waits_when_the_window_would_overflow(clock) -> None:
    pacer = TokenPacer(window_tokens=7500, window_s=60, clock=clock)

    first = pacer.reserve(5000, max_wait_s=300)
    second = pacer.reserve(5000, max_wait_s=300)
    pacer.settle(first.id, 1000)
    third = pacer.reserve(5000, max_wait_s=300)

    assert (first.wait_s, second.wait_s) == (0, 60)
    assert third.wait_s == 120  # the second booking (at t=60) blocks every window until t=120


def test_call_that_cannot_finish_before_the_deadline_is_not_made(settings, clock) -> None:
    client, fake = _client([{"title": "x"}], settings, clock)
    budget = _budget(settings, clock, deadline_s=settings.llm_call_timeout_s - 10)

    with pytest.raises(BudgetExhausted, match="deadline"):
        _ask(client, budget)
    assert fake.requests == []


def test_call_over_the_remaining_token_ceiling_is_not_made(settings, clock) -> None:
    client, fake = _client([{"title": "x"}], settings, clock)
    budget = _budget(settings, clock)
    budget.tokens_used = settings.request_token_ceiling - 1000

    with pytest.raises(BudgetExhausted, match="token ceiling"):
        _ask(client, budget)
    assert fake.requests == []
