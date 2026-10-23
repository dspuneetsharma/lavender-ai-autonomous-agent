"""End-to-end pipeline behaviour with FakeLLM (plan groups 1, 2, 3, 8 and 9)."""

from typing import get_args

import pytest
from docx import Document

from app.agent.review import REVIEW_PROMPT, ReviewIncomplete, review_and_repair
from app.config import Settings
from app.docx_builder import DocxRenderer, RenderError
from app.llm import RequestBudget, estimate_prompt_tokens
from app.schemas import Analysis, Plan, ProposalLabel, Section
from sample_data import (
    ANALYSIS,
    INJECTION,
    MINUTES_REQUEST,
    NO_ISSUES,
    OFF_TOPIC,
    SUMMARY,
    TABLE,
    material_issue,
)

BAD = "not json"


def _step(run, step_id: str):
    return next(report for report in run.result.response.plan if report.id == step_id)


# --------------------------------------------------------------------------- group 1: screening

def test_injection_is_refused_with_zero_llm_calls(run_pipeline) -> None:
    run = run_pipeline([], request=INJECTION)

    assert run.result.http_status == 400
    assert run.fake.requests == []
    assert run.result.document_name is None


def test_off_topic_request_makes_exactly_one_call_and_no_document(run_pipeline) -> None:
    run = run_pipeline([OFF_TOPIC], request="What is the best way to cook pasta?")

    assert run.result.http_status == 422
    assert run.result.response.message == "Not a document request: This is a cooking question."
    assert len(run.fake.requests) == 1
    assert run.rendered == []


# --------------------------------------------------------------------------- success path

def test_success_runs_analyze_tasks_and_one_review(run_pipeline) -> None:
    run = run_pipeline([ANALYSIS, SUMMARY, TABLE, NO_ISSUES])

    assert run.result.http_status == 200
    assert run.result.response.status == "success"
    assert len(run.fake.requests) == 4
    assert [s.heading for s in run.rendered[0]] == ["Summary", "Action Items"]
    assert run.result.response.usage.llm_calls == 4


# --------------------------------------------------------------------------- group 2: plan validation

def test_invalid_plan_is_replanned_once_then_fails(run_pipeline) -> None:
    bad_tasks = [{"id": "T1", "tool": "send_email", "title": "x", "purpose": "x", "covers": ["R1", "R2"]}]

    run = run_pipeline([{**ANALYSIS, "tasks": bad_tasks}, {"tasks": bad_tasks}])

    assert run.result.http_status == 502
    assert run.result.response.message.startswith("Could not build a valid plan")
    assert "unknown tool 'send_email'" in run.fake.requests[1][-1]["content"]
    assert len(run.fake.requests) == 2


def test_llm_optional_task_covering_a_requested_item_is_run_as_required(run_pipeline) -> None:
    optional_summary = {**ANALYSIS["tasks"][0], "required": False}
    analysis = {**ANALYSIS, "tasks": [optional_summary, ANALYSIS["tasks"][1]]}

    run = run_pipeline([analysis, BAD, BAD, BAD])

    assert run.result.http_status == 502
    assert run.result.response.message.startswith("Required task T1 'Summary' failed")


# --------------------------------------------------------------------------- group 3: task outcomes

def test_dependent_task_prompt_contains_the_prerequisite_output(run_pipeline) -> None:
    run = run_pipeline([ANALYSIS, SUMMARY, TABLE, NO_ISSUES])

    assert "Delay the launch to March 3" in run.fake.requests[2][-1]["content"]


def test_required_task_failure_fails_the_run_with_no_link(run_pipeline) -> None:
    run = run_pipeline([ANALYSIS, BAD, BAD, BAD])

    assert run.result.http_status == 502
    assert run.result.document_name is None
    assert [(r.id, r.status) for r in run.result.response.plan[:2]] == [("T1", "failed"), ("T2", "skipped")]


def test_required_task_skipped_for_time_fails_the_run(run_pipeline, settings: Settings) -> None:
    """Regression for the first live CRM failure: a required task skipped at the deadline."""
    tight = settings.model_copy(update={"request_deadline_s": settings.llm_call_timeout_s + 1})

    run = run_pipeline([ANALYSIS, SUMMARY], run_settings=tight, tokens_per_reply=5000)

    assert run.result.http_status == 502
    assert "skipped" in run.result.response.message
    assert "not enough time left" in run.result.response.message
    assert run.result.document_name is None


def test_optional_task_failure_makes_the_run_partial(run_pipeline) -> None:
    extra = {"id": "T3", "tool": "write_section", "title": "Next Steps", "purpose": "Extra",
             "required": False}
    analysis = {**ANALYSIS, "tasks": [*ANALYSIS["tasks"], extra]}

    run = run_pipeline([analysis, SUMMARY, TABLE, BAD, BAD, BAD, NO_ISSUES])

    assert run.result.http_status == 200
    assert run.result.response.status == "partial"
    assert "Optional section 'Next Steps' failed." in run.result.response.message


def test_unrequested_section_left_empty_by_repair_makes_the_run_partial_not_failed(run_pipeline) -> None:
    """Regression for live demo 2: an extra 'Conclusion' (covering nothing) was required by default."""
    conclusion = {"id": "T3", "tool": "write_section", "title": "Conclusion", "purpose": "Wrap up"}
    analysis = {**ANALYSIS, "tasks": [*ANALYSIS["tasks"], conclusion]}
    restated = {"paragraphs": ["The team reviewed launch readiness and agreed actions."], "items": []}
    emptied = {"paragraphs": [], "items": []}

    run = run_pipeline([analysis, SUMMARY, TABLE, restated, material_issue("T3"), emptied, NO_ISSUES])

    assert run.result.http_status == 200
    assert run.result.response.status == "partial"
    assert [s.heading for s in run.rendered[0]] == ["Summary", "Action Items"]
    assert "Optional section 'Conclusion' removed after review." in run.result.response.message


def test_planned_assumptions_task_is_not_rendered_twice(run_pipeline, settings: Settings) -> None:
    """Regression for live demo 2 attempt 3: a planned 'Assumptions' section duplicated the automatic one."""
    assumptions_request = {"id": "R3", "description": "Labelled assumptions", "quote": "labelled assumptions"}
    assumptions_task = {"id": "T3", "tool": "write_section", "title": "Assumptions", "purpose": "List them",
                        "covers": ["R3"]}
    analysis = {**ANALYSIS, "requested_items": [*ANALYSIS["requested_items"], assumptions_request],
                "tasks": [assumptions_task, *ANALYSIS["tasks"]]}

    run = run_pipeline([analysis, SUMMARY, TABLE, NO_ISSUES], renderer=DocxRenderer(settings.output_dir))

    assert run.result.response.status == "success"
    assert len(run.fake.requests) == 4  # no call was spent on the folded task
    headings = [p.text for p in Document(str(settings.output_dir / run.result.document_name)).paragraphs
                if p.style.name.startswith("Heading")]
    assert headings == ["Summary", "Action Items", "Assumptions"]


# --------------------------------------------------------------------------- group 8: review

def test_material_issue_is_repaired_and_verified(run_pipeline) -> None:
    run = run_pipeline([ANALYSIS, SUMMARY, TABLE, material_issue(), SUMMARY, NO_ISSUES])

    assert run.result.response.status == "success"
    assert _step(run, "repair").detail == "repaired T1"
    assert len(run.fake.requests) == 6


def test_verification_review_lists_the_other_sections(run_pipeline) -> None:
    run = run_pipeline([ANALYSIS, SUMMARY, TABLE, material_issue(), SUMMARY, NO_ISSUES])

    first_review, verification = run.fake.requests[3][-1]["content"], run.fake.requests[5][-1]["content"]
    assert "Other sections of the document" not in first_review  # every section is shown
    assert "Other sections of the document (not shown): Action Items" in verification


def test_review_prompt_lists_exactly_the_schema_labels() -> None:
    for label in get_args(ProposalLabel):
        assert f'"{label}"' in REVIEW_PROMPT


def test_unresolved_issue_in_a_required_section_fails(run_pipeline) -> None:
    run = run_pipeline([ANALYSIS, SUMMARY, TABLE, material_issue(), SUMMARY, material_issue()])

    assert run.result.http_status == 502
    assert run.result.response.message.startswith("Unresolved issue in required section 'Summary'")


def test_review_that_cannot_complete_fails_the_run(run_pipeline) -> None:
    run = run_pipeline([ANALYSIS, SUMMARY, TABLE, BAD, BAD, BAD])

    assert run.result.http_status == 502
    assert run.result.response.message.startswith("Review could not complete")
    assert _step(run, "review").status == "failed"


@pytest.fixture
def review_inputs() -> tuple[Analysis, Plan]:
    analysis = Analysis.model_validate(ANALYSIS)
    return analysis, Plan(tasks=analysis.tasks)


def _long_section(task_id: str, chars: int) -> Section:
    return Section(
        task_id=task_id, heading=f"Section {task_id}", covers=["R1"], paragraphs=["word " * (chars // 5)]
    )


def test_review_chunks_keep_every_reservation_within_the_cap(
    make_client, settings: Settings, review_inputs
) -> None:
    analysis, plan = review_inputs
    sections = [_long_section(f"T{n}", 4000) for n in range(1, 5)]
    client, fake = make_client([NO_ISSUES] * 4)

    review_and_repair(sections, plan, analysis, MINUTES_REQUEST, client,
                      RequestBudget.from_settings(settings), settings)

    assert len(fake.requests) >= 2  # the sections did not fit in one review call
    for messages in fake.requests:  # as sent, schema included
        reserved = estimate_prompt_tokens(messages, settings) + settings.completion_tokens_task
        assert reserved <= settings.per_call_token_cap


def test_single_section_too_large_to_review_fails(make_client, settings: Settings, review_inputs) -> None:
    analysis, plan = review_inputs
    client, fake = make_client([NO_ISSUES])

    with pytest.raises(ReviewIncomplete, match="too large to review"):
        review_and_repair([_long_section("T1", 20000)], plan, analysis, MINUTES_REQUEST, client,
                          RequestBudget.from_settings(settings), settings)
    assert fake.requests == []


# --------------------------------------------------------------------------- group 9: gate and render

def test_gate_failure_returns_no_link(run_pipeline) -> None:
    unlabelled = {"paragraphs": ["The team reviewed launch readiness."],
                  "items": [{"type": "point", "kind": "proposal", "text": "Hold a retrospective"}]}

    run = run_pipeline([ANALYSIS, unlabelled, TABLE, NO_ISSUES])

    assert run.result.http_status == 502
    assert run.result.response.message.startswith("Completeness gate failed")
    assert run.result.document_name is None
    assert run.rendered == []


def test_render_failure_returns_no_link(run_pipeline) -> None:
    def broken_renderer(_analysis, _sections) -> str:
        raise RenderError("disk full")

    run = run_pipeline([ANALYSIS, SUMMARY, TABLE, NO_ISSUES], renderer=broken_renderer)

    assert run.result.http_status == 502
    assert run.result.response.message == "Rendering the document failed: disk full"
    assert run.result.document_name is None
    assert _step(run, "render").status == "failed"
