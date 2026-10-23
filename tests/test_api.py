"""HTTP API: status codes, download, path traversal and request validation (plan groups 1 and 9)."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.agent.pipeline import AgentPipeline
from app.config import Settings, get_settings
from app.docx_builder import DocxRenderer
from app.main import DOCX_MEDIA_TYPE, create_app
from sample_data import ANALYSIS, INJECTION, MINUTES_REQUEST, NO_ISSUES, OFF_TOPIC, SUMMARY, TABLE


@pytest.fixture
def api(settings: Settings, make_client) -> Callable[[list[object]], TestClient]:
    """A TestClient whose pipeline answers with the given scripted replies and writes real .docx files."""

    @contextmanager
    def start(replies: list[object]) -> Iterator[TestClient]:
        client, _ = make_client(replies)
        pipeline = AgentPipeline(client, DocxRenderer(settings.output_dir), settings)
        with TestClient(create_app(settings, pipeline)) as test_client:
            yield test_client

    return start


def test_success_returns_a_link_that_downloads_the_document(api) -> None:
    with api([ANALYSIS, SUMMARY, TABLE, NO_ISSUES]) as client:
        body = client.post("/agent", json={"request": MINUTES_REQUEST}).json()
        download = client.get(body["document_url"])

    assert body["status"] == "success"
    assert download.status_code == 200
    assert download.headers["content-type"] == DOCX_MEDIA_TYPE
    assert 'filename="product-sync-minutes.docx"' in download.headers["content-disposition"]


@pytest.mark.parametrize("name", [
    "..%2F..%2F.env",
    "..%5Capp%5Cmain.py",
    "x.docx",
    "0" * 32 + "-missing.docx",
])
def test_only_generated_documents_can_be_downloaded(api, name: str) -> None:
    with api([]) as client:
        assert client.get(f"/documents/{name}").status_code == 404


@pytest.mark.parametrize("body", [
    {"request": "   "},
    {"request": "x" * (get_settings().max_request_chars + 1)},
    {},
])
def test_empty_too_long_or_missing_request_is_rejected_before_any_llm_call(api, body: dict) -> None:
    with api([]) as client:
        response = client.post("/agent", json=body)

    assert response.status_code == 422
    assert "status" not in response.json()  # FastAPI validation error, the agent never ran


def test_injection_is_refused_with_400_and_no_call(api) -> None:
    with api([]) as client:
        body = client.post("/agent", json={"request": INJECTION}).json()

    assert body["status"] == "failed"
    assert body["usage"]["llm_calls"] == 0


def test_off_topic_returns_422_with_no_link(api) -> None:
    with api([OFF_TOPIC]) as client:
        response = client.post("/agent", json={"request": "How do I cook pasta?"})

    assert response.status_code == 422
    assert response.json()["document_url"] is None


def test_failed_run_returns_502_with_no_link(api) -> None:
    with api([ANALYSIS, "bad", "bad", "bad"]) as client:
        response = client.post("/agent", json={"request": MINUTES_REQUEST})

    assert response.status_code == 502
    assert response.json()["document_url"] is None


def test_health_reports_the_request_limit_without_an_llm_call(api, settings: Settings) -> None:
    with api([]) as client:
        health = client.get("/health").json()

    assert health == {"status": "ok", "model": settings.groq_model,
                      "max_request_chars": settings.max_request_chars}
