"""LLM access layer: token estimation, rate pacing, per-request budget, retries and JSON validation.

`LLMClient.chat_json` is the only entry point the agent uses. It talks to a `Transport`:
`GroqTransport` in production, `FakeLLM` in tests, so pacing and retry logic is the same in both.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

import groq
from pydantic import BaseModel, ValidationError

from app.config import Settings

logger = logging.getLogger(__name__)

Message = dict[str, str]
ModelT = TypeVar("ModelT", bound=BaseModel)
Clock = Callable[[], float]
Sleep = Callable[[float], None]

_CONTEXT_LIMIT_MARKERS = ("context_length_exceeded", "reduce the length", "request too large")
_INVALID_JSON_MARKER = "json_validate_failed"
# Groq's 429 for a used-up daily quota, e.g. "Rate limit reached ... on tokens per day (TPD) ...".
_DAILY_LIMIT_MARKERS = ("per day", "(tpd)", "(rpd)")
_TRY_AGAIN_IN = re.compile(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?")
_MAX_REPORTED_VALIDATION_ERRORS = 3


# --------------------------------------------------------------------------- errors


class LLMError(Exception):
    """Base class for LLM failures. `reason` is short and safe to show to the user."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BudgetExhausted(LLMError):
    """The call was not made: the token ceiling or deadline would be exceeded."""


class ContextTooLarge(LLMError):
    """The prompt exceeds the per-call cap or the provider rejected its size. It is never resent."""


class LLMCallFailed(LLMError):
    """Calls were made but no valid output was obtained within the retry bounds."""


class DailyLimitReached(Exception):
    """The provider's daily quota is used up, so the whole request stops at once.

    Not an `LLMError`: no task, review or retry handles it; the pipeline turns it into a clear HTTP 429.
    """

    def __init__(self, retry_after_s: float | None) -> None:
        wait = f"in {_describe_wait(retry_after_s)}" if retry_after_s else "once the daily limit resets"
        self.reason = (
            "Today's free usage limit for the AI model has been used up. "
            f"It resets automatically; please try again {wait}."
        )
        super().__init__(self.reason)
        self.retry_after_s = retry_after_s


class RateLimited(Exception):
    """Transport signal: the provider answered 429. `daily` means the daily quota, not the per-minute one."""

    def __init__(self, retry_after_s: float | None, *, daily: bool = False) -> None:
        super().__init__(f"rate limited (retry after {retry_after_s}s{', daily limit' if daily else ''})")
        self.retry_after_s = retry_after_s
        self.daily = daily


class ProviderError(Exception):
    """Transport signal: the provider call failed; `retryable` says whether another attempt may help."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


# --------------------------------------------------------------------------- estimation


def estimate_prompt_tokens(messages: Sequence[Message], settings: Settings) -> int:
    """Approximate prompt tokens from the full serialized request, with a safety margin."""
    serialized = json.dumps(list(messages), ensure_ascii=False)
    raw = len(serialized) / settings.estimate_chars_per_token
    raw += settings.estimate_tokens_per_message * len(messages)
    return math.ceil(raw * (1 + settings.estimate_margin))


# --------------------------------------------------------------------------- pacing


@dataclass(frozen=True)
class Reservation:
    """A booked slot in the pacer window."""

    id: int
    wait_s: float


class TokenPacer:
    """Process-wide sliding-window limiter that keeps booked tokens per window under the limit.

    Reservations may be booked in the future (after a wait); they count against every window
    they fall into, so concurrent requests never overbook.
    """

    def __init__(self, window_tokens: int, window_s: float, clock: Clock = time.monotonic) -> None:
        self._window_tokens = window_tokens
        self._window_s = window_s
        self._clock = clock
        self._lock = threading.Lock()
        self._booked: dict[int, tuple[float, int]] = {}
        self._next_id = 0

    def reserve(self, tokens: int, max_wait_s: float) -> Reservation | None:
        """Book `tokens` at the earliest slot; return None if that slot is more than `max_wait_s` away."""
        if tokens > self._window_tokens:
            raise ValueError("A single reservation cannot exceed the window limit")
        with self._lock:
            now = self._clock()
            self._forget_expired(now)
            start = self._earliest_start(now, tokens)
            if start - now > max_wait_s:
                return None
            reservation = Reservation(id=self._next_id, wait_s=start - now)
            self._booked[reservation.id] = (start, tokens)
            self._next_id += 1
            return reservation

    def settle(self, reservation_id: int, actual_tokens: int) -> None:
        """Replace a reservation's estimate with the provider's real usage."""
        with self._lock:
            if reservation_id in self._booked:
                start, _ = self._booked[reservation_id]
                self._booked[reservation_id] = (start, actual_tokens)

    def release(self, reservation_id: int) -> None:
        """Drop a reservation the provider never processed."""
        with self._lock:
            self._booked.pop(reservation_id, None)

    def _forget_expired(self, now: float) -> None:
        expired = [rid for rid, (start, _) in self._booked.items() if start + self._window_s <= now]
        for rid in expired:
            del self._booked[rid]

    def _tokens_in_window_at(self, moment: float) -> int:
        return sum(tokens for start, tokens in self._booked.values() if start > moment - self._window_s)

    def _earliest_start(self, now: float, tokens: int) -> float:
        expiries = (start + self._window_s for start, _ in self._booked.values())
        for candidate in sorted({now, *expiries}):
            if self._tokens_in_window_at(candidate) + tokens <= self._window_tokens:
                return candidate
        raise AssertionError("unreachable: the last expiry always leaves an empty window")


# --------------------------------------------------------------------------- per-request budget


@dataclass
class CallRecord:
    """One provider attempt, kept in the request ledger."""

    purpose: str
    attempt: int
    reserved_tokens: int
    charged_tokens: int
    duration_s: float
    outcome: str


@dataclass
class RequestBudget:
    """Token ceiling, deadline and call ledger for a single agent request."""

    token_ceiling: int
    deadline_s: float
    clock: Clock = time.monotonic
    tokens_used: int = 0
    calls: list[CallRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._started = self.clock()

    @classmethod
    def from_settings(cls, settings: Settings, clock: Clock = time.monotonic) -> RequestBudget:
        return cls(
            token_ceiling=settings.request_token_ceiling,
            deadline_s=settings.request_deadline_s,
            clock=clock,
        )

    @property
    def elapsed_s(self) -> float:
        return self.clock() - self._started

    @property
    def time_left_s(self) -> float:
        return self.deadline_s - self.elapsed_s

    @property
    def tokens_left(self) -> int:
        return self.token_ceiling - self.tokens_used

    def record(self, call: CallRecord) -> None:
        self.tokens_used += call.charged_tokens
        self.calls.append(call)


# --------------------------------------------------------------------------- transports


@dataclass(frozen=True)
class Completion:
    """Raw provider reply. `total_tokens` is None when the provider reported no usage."""

    content: str
    finish_reason: str
    total_tokens: int | None


class Transport(Protocol):
    """Sends one chat request. Raises RateLimited, ContextTooLarge or ProviderError on failure."""

    def complete(
        self, messages: Sequence[Message], max_completion_tokens: int, timeout_s: float
    ) -> Completion: ...


class GroqTransport:
    """Groq chat completions in JSON mode, with SDK retries disabled so the client controls them."""

    def __init__(self, api_key: str, model: str, reasoning_effort: str) -> None:
        self._client = groq.Groq(api_key=api_key, max_retries=0)
        self._model = model
        self._reasoning_effort = reasoning_effort

    def complete(
        self, messages: Sequence[Message], max_completion_tokens: int, timeout_s: float
    ) -> Completion:
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=list(messages),
                max_completion_tokens=max_completion_tokens,
                response_format={"type": "json_object"},
                reasoning_effort=self._reasoning_effort,
                include_reasoning=False,
                timeout=timeout_s,
            )
        except groq.RateLimitError as exc:
            raise rate_limited_from(exc) from exc
        except groq.APIStatusError as exc:
            raise _map_status_error(exc) from exc
        except groq.APITimeoutError as exc:
            raise ProviderError("provider call timed out", retryable=True) from exc
        except groq.APIConnectionError as exc:
            raise ProviderError("could not reach the provider", retryable=True) from exc

        choice = response.choices[0]
        usage = response.usage
        return Completion(
            content=choice.message.content or "",
            finish_reason=choice.finish_reason,
            total_tokens=usage.total_tokens if usage else None,
        )


def rate_limited_from(exc: groq.RateLimitError) -> RateLimited:
    """Tell a used-up daily quota from the per-minute limit, and find how long to wait."""
    details = f"{exc.message} {exc.body}".lower()
    daily = any(marker in details for marker in _DAILY_LIMIT_MARKERS)
    return RateLimited(_retry_after_s(exc) or _try_again_in_s(details), daily=daily)


def _retry_after_s(exc: groq.RateLimitError) -> float | None:
    value = exc.response.headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def _try_again_in_s(details: str) -> float | None:
    """Read "try again in 1h2m3.5s" from the error text; None if it isn't there."""
    match = _TRY_AGAIN_IN.search(details)
    if match is None or not any(match.groups()):
        return None
    hours, minutes, seconds = (float(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _describe_wait(seconds: float) -> str:
    minutes = math.ceil(seconds / 60)
    if minutes <= 1:
        return "about a minute"
    if minutes < 60:
        return f"about {minutes} minutes"
    hours, rest = divmod(minutes, 60)
    hours_text = "1 hour" if hours == 1 else f"{hours} hours"
    return f"about {hours_text}" + (f" {rest} minutes" if rest else "")


def _map_status_error(exc: groq.APIStatusError) -> Exception:
    details = f"{exc.message} {exc.body}".lower()
    too_long = exc.status_code == 400 and any(marker in details for marker in _CONTEXT_LIMIT_MARKERS)
    if exc.status_code == 413 or too_long:
        return ContextTooLarge("the provider rejected the request as too large")
    if exc.status_code == 400 and _INVALID_JSON_MARKER in details:
        return ProviderError("the model produced invalid JSON", retryable=True)
    if exc.status_code >= 500:
        return ProviderError(f"provider error {exc.status_code}", retryable=True)
    return ProviderError(f"provider rejected the request ({exc.status_code})", retryable=False)


class FakeLLM:
    """Scripted transport for tests: returns queued replies in order and records every request.

    A reply may be a Pydantic model or dict (sent as JSON), a raw string, a `Completion`,
    or an exception to raise.
    """

    def __init__(
        self,
        replies: Sequence[BaseModel | dict | str | Completion | Exception] = (),
        tokens_per_reply: int = 100,
    ) -> None:
        self._replies = deque(replies)
        self._tokens_per_reply = tokens_per_reply
        self.requests: list[list[Message]] = []

    def complete(
        self, messages: Sequence[Message], max_completion_tokens: int, timeout_s: float
    ) -> Completion:
        self.requests.append(list(messages))
        if not self._replies:
            raise ProviderError("FakeLLM has no scripted reply left", retryable=False)
        reply = self._replies.popleft()
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, Completion):
            return reply
        if isinstance(reply, BaseModel):
            content = reply.model_dump_json()
        elif isinstance(reply, dict):
            content = json.dumps(reply)
        else:
            content = reply
        return Completion(content=content, finish_reason="stop", total_tokens=self._tokens_per_reply)


# --------------------------------------------------------------------------- client


class LLMClient:
    """Budget-aware JSON chat: checks caps, paces, retries within bounds and validates the reply."""

    def __init__(
        self,
        transport: Transport,
        pacer: TokenPacer,
        settings: Settings,
        clock: Clock = time.monotonic,
        sleep: Sleep = time.sleep,
    ) -> None:
        self._transport = transport
        self._pacer = pacer
        self._settings = settings
        self._clock = clock
        self._sleep = sleep

    def reservation_tokens(
        self, messages: Sequence[Message], schema: type[BaseModel], max_completion_tokens: int
    ) -> int:
        """Tokens a call would reserve (prompt incl. schema, plus completion cap), without sending it."""
        return self._reservation(_with_schema_instructions(messages, schema), max_completion_tokens)

    def chat_json(
        self,
        messages: Sequence[Message],
        schema: type[ModelT],
        *,
        purpose: str,
        max_completion_tokens: int,
        budget: RequestBudget,
    ) -> ModelT:
        """Send `messages` and return the reply parsed as `schema`.

        Raises BudgetExhausted (call not made), ContextTooLarge (not sent / not resent),
        LLMCallFailed (retries exhausted) or DailyLimitReached (the provider's daily quota is used up).
        """
        base_messages = _with_schema_instructions(messages, schema)
        feedback: str | None = None
        attempt = 0
        rate_limit_waits = 0
        last_error = ""

        while attempt <= self._settings.llm_schema_retries:
            attempt_messages = base_messages if feedback is None else [*base_messages, _user(feedback)]
            try:
                completion, record = self._send(
                    attempt_messages, max_completion_tokens, purpose, attempt + 1, budget
                )
            except RateLimited as exc:
                rate_limit_waits += 1
                self._wait_for_rate_limit(exc, rate_limit_waits, purpose, budget)
                continue
            except ProviderError as exc:
                if not exc.retryable:
                    raise LLMCallFailed(f"{purpose}: {exc}") from exc
                attempt += 1
                last_error = str(exc)
                logger.warning("%s: attempt %d failed: %s", purpose, attempt, exc)
                continue

            attempt += 1
            if completion.finish_reason == "length":
                record.outcome = "truncated"
                last_error = "output truncated at the completion token limit"
                feedback = (
                    "Your previous reply was cut off. "
                    "Reply again, more concisely, with only the JSON object."
                )
            else:
                try:
                    return schema.model_validate_json(completion.content)
                except ValidationError as exc:
                    record.outcome = "invalid_output"
                    last_error = _summarize_validation_error(exc)
                    feedback = (
                        f"Your previous reply was rejected: {last_error}. "
                        "Reply again with only a JSON object that matches the schema."
                    )
            logger.warning("%s: attempt %d rejected: %s", purpose, attempt, last_error)

        raise LLMCallFailed(f"{purpose}: no valid output after {attempt} attempts ({last_error})")

    def _send(
        self,
        messages: Sequence[Message],
        max_completion_tokens: int,
        purpose: str,
        attempt: int,
        budget: RequestBudget,
    ) -> tuple[Completion, CallRecord]:
        """Check limits, wait for the pacer, make one provider call and record it in the ledger."""
        reserved = self._reservation(messages, max_completion_tokens)
        if reserved > self._settings.per_call_token_cap:
            raise ContextTooLarge(
                f"{purpose}: context too large "
                f"(about {reserved} tokens, cap {self._settings.per_call_token_cap})"
            )
        if reserved > budget.tokens_left:
            raise BudgetExhausted(f"{purpose}: token ceiling reached ({budget.tokens_left} tokens left)")

        max_wait_s = budget.time_left_s - self._settings.llm_call_timeout_s
        reservation = self._pacer.reserve(reserved, max_wait_s=max_wait_s)
        if reservation is None:
            raise BudgetExhausted(f"{purpose}: not enough time left before the deadline")
        if reservation.wait_s > 0:
            logger.info("%s: pacing %.1fs to stay under the per-minute limit", purpose, reservation.wait_s)
            self._sleep(reservation.wait_s)

        record = CallRecord(purpose, attempt, reserved, charged_tokens=reserved, duration_s=0.0, outcome="ok")
        started = self._clock()
        try:
            completion = self._transport.complete(
                messages, max_completion_tokens, self._settings.llm_call_timeout_s
            )
        except (RateLimited, ContextTooLarge) as exc:
            self._pacer.release(reservation.id)
            record.charged_tokens = 0
            record.outcome = "rate_limited" if isinstance(exc, RateLimited) else "context_too_large"
            raise
        except ProviderError:
            record.outcome = "provider_error"  # usage unknown: keep the full reservation charged
            raise
        else:
            if completion.total_tokens is not None:
                record.charged_tokens = completion.total_tokens
                self._pacer.settle(reservation.id, completion.total_tokens)
            return completion, record
        finally:
            record.duration_s = self._clock() - started
            budget.record(record)
            logger.info(
                "%s: attempt %d %s, %d tokens, %.1fs",
                purpose, attempt, record.outcome, record.charged_tokens, record.duration_s,
            )

    def _reservation(self, messages: Sequence[Message], max_completion_tokens: int) -> int:
        return estimate_prompt_tokens(messages, self._settings) + max_completion_tokens

    def _wait_for_rate_limit(self, exc: RateLimited, waits: int, purpose: str, budget: RequestBudget) -> None:
        """Honour retry-after within the wait limit and the deadline; a daily limit stops at once."""
        if exc.daily:
            logger.warning("%s: daily usage limit reached; stopping the request", purpose)
            raise DailyLimitReached(exc.retry_after_s)
        if waits > self._settings.llm_max_rate_limit_waits:
            raise LLMCallFailed(f"{purpose}: still rate limited after {waits - 1} waits")
        wait_s = exc.retry_after_s
        if wait_s is None:
            wait_s = self._settings.llm_rate_limit_fallback_wait_s
        if wait_s + self._settings.llm_call_timeout_s > budget.time_left_s:
            raise BudgetExhausted(f"{purpose}: rate-limit wait would pass the deadline")
        logger.warning("%s: rate limited, waiting %.1fs", purpose, wait_s)
        self._sleep(wait_s)


def build_llm_client(settings: Settings) -> LLMClient:
    """Create the production client. Fails clearly if the API key is missing."""
    if settings.groq_api_key is None:
        raise RuntimeError("GROQ_API_KEY is not set. Add it to .env (see .env.example).")
    transport = GroqTransport(
        api_key=settings.groq_api_key.get_secret_value(),
        model=settings.groq_model,
        reasoning_effort=settings.groq_reasoning_effort,
    )
    pacer = TokenPacer(settings.pacer_window_tokens, settings.pacer_window_s)
    return LLMClient(transport, pacer, settings)


# --------------------------------------------------------------------------- helpers


def _user(content: str) -> Message:
    return {"role": "user", "content": content}


def _with_schema_instructions(messages: Sequence[Message], schema: type[BaseModel]) -> list[Message]:
    """Attach the JSON schema to the system message so it is part of every estimate and request."""
    schema_json = json.dumps(_strip_schema_titles(schema.model_json_schema()), separators=(",", ":"))
    instruction = f"Respond with a single JSON object that matches this JSON Schema:\n{schema_json}"
    if messages and messages[0]["role"] == "system":
        first = {"role": "system", "content": f"{messages[0]['content']}\n\n{instruction}"}
        return [first, *messages[1:]]
    return [{"role": "system", "content": instruction}, *messages]


def _strip_schema_titles(node: object, is_name_map: bool = False) -> object:
    """Drop Pydantic's auto-generated "title" annotations to save prompt tokens.

    Keys inside `properties`/`$defs` are field or model names, so a field called "title" is kept.
    """
    if isinstance(node, dict):
        return {
            key: _strip_schema_titles(value, is_name_map=key in ("properties", "$defs") and not is_name_map)
            for key, value in node.items()
            if is_name_map or key != "title"
        }
    if isinstance(node, list):
        return [_strip_schema_titles(value) for value in node]
    return node


def _summarize_validation_error(exc: ValidationError) -> str:
    errors = exc.errors(include_url=False)[:_MAX_REPORTED_VALIDATION_ERRORS]
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'root'}: {e['msg']}" for e in errors)
