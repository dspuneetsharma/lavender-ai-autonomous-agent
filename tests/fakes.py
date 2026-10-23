"""Test doubles that are not LLM replies."""


class FakeClock:
    """A controllable clock: `sleep` advances time instantly and records each wait."""

    def __init__(self) -> None:
        self.now_s = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now_s

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now_s += seconds
