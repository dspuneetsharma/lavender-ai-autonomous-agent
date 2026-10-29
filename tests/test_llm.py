"""LLM wrapper: retries with feedback, no resend on 413, pacing, deadline, token ceiling (plan groups 4-5)."""

import groq
import httpx
import pytest
from pydantic import BaseModel

from app.config import Settings
from app.llm import (
    BudgetExhausted,
    Completion,
    ContextTooLarge,
    DailyLimitReached,
    FakeLLM,
    LLMCallFailed,
    LLMClient,
    RateLimited,
    RequestBudget,
    TokenPacer,
    rate_limited_from,
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


DAILY_LIMIT_TEXT = (
    "Rate limit reached for model `openai/gpt-oss-120b` in organization `org_x` service tier `on_demand` "
    "on tokens per day (TPD): Limit 200000, Used 199500, Requested 3200. Please try again in 14m2.5s."
)
MINUTE_LIMIT_TEXT = (
    "Rate limit reached for model `openai/gpt-oss-120b` on tokens per minute (TPM): Limit 8000, "
    "Used 7900, Requested 900. Please try again in 6.75s."
)


def _groq_429(message: str, retry_after: str | None = None) -> groq.RateLimitError:
    headers = {"retry-after": retry_after} if retry_after else {}
    response = httpx.Response(429, headers=headers, request=httpx.Request("POST", "https://api.groq.com"))
    return groq.RateLimitError(message, response=response, body={"error": {"message": message}})


def test_daily_limit_stops_at_once_with_a_clear_message(settings, clock) -> None:
    client, fake = _client([RateLimited(842.5, daily=True), {"title": "never"}], settings, clock)

    with pytest.raises(DailyLimitReached) as caught:
        _ask(client, _budget(settings, clock))

    assert clock.sleeps == []
    assert len(fake.requests) == 1
    assert caught.value.reason == (
        "Today's free usage limit for the AI model has been used up. "
        "It resets automatically; please try again in about 15 minutes."
    )


def test_daily_limit_without_a_wait_time_says_it_resets() -> None:
    assert DailyLimitReached(None).reason.endswith("please try again once the daily limit resets.")
    assert "in about 2 hours 5 minutes" in DailyLimitReached(7445).reason


def test_groq_daily_429_is_told_apart_from_the_per_minute_one() -> None:
    daily = rate_limited_from(_groq_429(DAILY_LIMIT_TEXT))
    minute = rate_limited_from(_groq_429(MINUTE_LIMIT_TEXT, retry_after="7"))

    assert (daily.daily, daily.retry_after_s) == (True, 842.5)  # read from "try again in 14m2.5s"
    assert (minute.daily, minute.retry_after_s) == (False, 7.0)  # the header wins when present


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
