"""Shared fixtures: settings independent of .env, a fake clock, and FakeLLM-backed clients and pipelines.

`tests/` is on the import path (pytest.ini), so test modules import `fakes` and `sample_data` directly.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import pytest

from app.agent.pipeline import AgentPipeline, PipelineResult, Renderer
from app.config import Settings
from app.llm import FakeLLM, LLMClient, TokenPacer
from app.schemas import Analysis, Section
from fakes import FakeClock
from sample_data import MINUTES_REQUEST


@dataclass
class PipelineRun:
    """What a pipeline test inspects: the result, every prompt sent, and what reached the renderer."""

    result: PipelineResult
    fake: FakeLLM
    rendered: list[list[Section]] = field(default_factory=list)


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Code defaults only (no .env), writing documents to a temporary folder."""
    return Settings(_env_file=None, output_dir=tmp_path)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_client(settings: Settings) -> Callable[..., tuple[LLMClient, FakeLLM]]:
    """Build an LLMClient over a FakeLLM; sleeping is instant so pacing never slows the tests."""

    def make(
        replies: Sequence[object], tokens_per_reply: int = 100, client_settings: Settings | None = None
    ) -> tuple[LLMClient, FakeLLM]:
        current = client_settings or settings
        fake = FakeLLM(replies, tokens_per_reply=tokens_per_reply)
        pacer = TokenPacer(current.pacer_window_tokens, current.pacer_window_s)
        return LLMClient(fake, pacer, current, sleep=lambda _: None), fake

    return make


@pytest.fixture
def run_pipeline(settings: Settings, make_client) -> Callable[..., PipelineRun]:
    """Run the full pipeline on scripted replies with a recording renderer (unless one is given)."""

    def run(
        replies: Sequence[object],
        request: str = MINUTES_REQUEST,
        renderer: Renderer | None = None,
        run_settings: Settings | None = None,
        tokens_per_reply: int = 100,
    ) -> PipelineRun:
        current = run_settings or settings
        client, fake = make_client(replies, tokens_per_reply, current)
        rendered: list[list[Section]] = []

        def recording_renderer(_: Analysis, sections: Sequence[Section]) -> str:
            rendered.append(list(sections))
            return "document.docx"

        pipeline = AgentPipeline(client, renderer or recording_renderer, current)
        return PipelineRun(pipeline.run(request), fake, rendered)

    return run
