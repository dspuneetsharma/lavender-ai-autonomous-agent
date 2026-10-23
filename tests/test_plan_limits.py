"""Plan size limits: oversized plans are grouped before any section is written, without dropping items."""

import pytest

from app.agent.pipeline import AgentPipeline
from app.config import Settings
from app.guardrails import dependency_order, merge_to_limit, validate_plan
from app.llm import FakeLLM, LLMClient, TokenPacer
from app.schemas import Analysis, Plan, Section
from app.tools import TOOLS

REQUEST = (
    "Create a project plan to migrate our CRM to the cloud. Assess feasibility, identify conflicting "
    "requirements, and propose phases, responsibilities by role, a timeline, potential risks and "
    "mitigations, and decisions needed before execution."
)
REQUESTED = [
    ("R1", "Feasibility assessment", "Assess feasibility"),
    ("R2", "Conflicting requirements", "identify conflicting requirements"),
    ("R3", "Phases", "phases"),
    ("R4", "Responsibilities by role", "responsibilities by role"),
    ("R5", "Timeline", "a timeline"),
    ("R6", "Risks and mitigations", "potential risks and mitigations"),
    ("R7", "Decisions needed before execution", "decisions needed before execution"),
]


def _task(task_id: str, title: str, covers: list[str]) -> dict:
    return {"id": task_id, "tool": "write_section", "title": title, "purpose": title, "covers": covers}


ONE_TASK_PER_ITEM = [_task(f"T{n}", description, [item_id])
                     for n, (item_id, description, _) in enumerate(REQUESTED, start=1)]
GROUPED = ONE_TASK_PER_ITEM[:5] + [_task("T6", "Risks, Mitigations & Decisions Needed", ["R6", "R7"])]
ANALYSIS = {
    "is_document_request": True,
    "title": "CRM Cloud Migration Plan",
    "document_type": "project plan",
    "category": "planning",
    "requested_items": [{"id": i, "description": d, "quote": q} for i, d, q in REQUESTED],
    "tasks": ONE_TASK_PER_ITEM,
}


@pytest.fixture
def analysis() -> Analysis:
    return Analysis.model_validate(ANALYSIS)


def test_defaults_fit_the_free_tier(settings: Settings) -> None:
    assert settings.max_plan_tasks == 6
    assert settings.request_deadline_s == 480
    assert settings.request_token_ceiling == 40_000
    assert settings.llm_call_timeout_s == 30


def test_plan_over_the_task_limit_is_rejected(settings: Settings, analysis: Analysis) -> None:
    errors = validate_plan(Plan(tasks=ONE_TASK_PER_ITEM), analysis, TOOLS.keys(), settings)

    assert errors == ["The plan must have 2-6 tasks; it has 7."]


def test_plan_under_the_task_limit_explains_the_automatic_assumptions_section(
    settings: Settings, analysis: Analysis
) -> None:
    one_task = Plan(tasks=[_task("T1", "Whole plan", [item_id for item_id, _, _ in REQUESTED])])

    errors = validate_plan(one_task, analysis, TOOLS.keys(), settings)

    assert errors[0] == "The plan must have 2-6 tasks; it has 1."
    assert "an Assumptions-only section does not count" in errors[1]


def test_grouped_plan_keeps_every_requested_item(settings: Settings, analysis: Analysis) -> None:
    assert validate_plan(Plan.model_validate({"tasks": GROUPED}), analysis, TOOLS.keys(), settings) == []


def test_grouping_that_drops_a_requested_item_is_rejected(settings: Settings, analysis: Analysis) -> None:
    dropped = ONE_TASK_PER_ITEM[:6]

    errors = validate_plan(Plan.model_validate({"tasks": dropped}), analysis, TOOLS.keys(), settings)

    assert errors == ["Requested items not covered by any task: R7."]


def test_oversized_plan_is_regrouped_before_any_section_is_written(settings: Settings) -> None:
    section = {"paragraphs": ["This section sets out the proposed approach for the CRM cloud migration."],
               "items": []}
    no_issues = {"issues": []}
    fake = FakeLLM([ANALYSIS, {"tasks": GROUPED}, *[section] * 6, no_issues, no_issues])
    client = LLMClient(fake, TokenPacer(settings.pacer_window_tokens, settings.pacer_window_s), settings,
                       sleep=lambda _: None)
    rendered: list[Section] = []

    def renderer(_: Analysis, sections: list[Section]) -> str:
        rendered.extend(sections)
        return "plan.docx"

    result = AgentPipeline(client, renderer, settings).run(REQUEST)

    assert result.response.status == "success"
    assert "fixing a document plan" in fake.requests[1][0]["content"]
    assert "it has 7" in fake.requests[1][-1]["content"]
    assert [s.heading for s in rendered][-1] == "Risks, Mitigations & Decisions Needed"
    assert {item_id for s in rendered for item_id in s.covers} == {item_id for item_id, _, _ in REQUESTED}


def test_merge_to_limit_keeps_every_covered_item(settings: Settings, analysis: Analysis) -> None:
    merged = merge_to_limit(Plan.model_validate({"tasks": ONE_TASK_PER_ITEM}), settings.max_plan_tasks)

    assert [task.id for task in merged.tasks] == ["T1", "T2", "T3", "T4", "T5", "T6"]
    assert merged.tasks[-1].title == "Risks and mitigations & Decisions needed before execution"
    assert merged.tasks[-1].covers == ["R6", "R7"]
    assert validate_plan(merged, analysis, TOOLS.keys(), settings) == []


def test_merge_to_limit_redirects_dependencies_and_never_mixes_tools() -> None:
    plan = Plan.model_validate({"tasks": [
        _task("T1", "Overview", ["R1"]),
        _task("T2", "Phases", ["R3"]),
        {**_task("T3", "Timeline", ["R5"]), "tool": "build_table", "depends_on": ["T2"]},
    ]})

    merged = merge_to_limit(plan, 2)

    assert [(task.id, task.tool, task.covers) for task in merged.tasks] == [
        ("T1", "write_section", ["R1", "R3"]),
        ("T3", "build_table", ["R5"]),
    ]
    assert merged.tasks[1].depends_on == ["T1"]


def test_replan_that_still_exceeds_the_limit_is_merged_in_code(settings: Settings) -> None:
    """Regression: the live CRM request failed because the analyze plan and the re-plan both had 7 tasks."""
    section = {"paragraphs": ["This section sets out the proposed approach for the CRM cloud migration."],
               "items": []}
    no_issues = {"issues": []}
    fake = FakeLLM([ANALYSIS, {"tasks": ONE_TASK_PER_ITEM}, *[section] * 6, no_issues, no_issues])
    client = LLMClient(fake, TokenPacer(settings.pacer_window_tokens, settings.pacer_window_s), settings,
                       sleep=lambda _: None)
    rendered: list[Section] = []

    def renderer(_: Analysis, sections: list[Section]) -> str:
        rendered.extend(sections)
        return "plan.docx"

    result = AgentPipeline(client, renderer, settings).run(REQUEST)

    assert "a plan of 2-6 tasks" in fake.requests[0][0]["content"]
    assert "list of 2-6 tasks" in fake.requests[1][0]["content"]
    assert "The plan must have 2-6 tasks; it has 7." in fake.requests[1][-1]["content"]
    assert result.response.status == "success"
    assert len(rendered) == 6
    assert {item_id for s in rendered for item_id in s.covers} == {item_id for item_id, _, _ in REQUESTED}


def _plan(*tasks: dict) -> Plan:
    return Plan.model_validate({"tasks": list(tasks)})


def _table(task_id: str, title: str, covers: list[str], **extra: object) -> dict:
    return {**_task(task_id, title, covers), "tool": "build_table", **extra}


def test_merge_keeps_both_purposes_covers_and_prerequisites_and_is_required_if_either_was() -> None:
    plan = _plan(
        _table("T1", "Budget", ["R1"]),
        _table("T2", "Team", ["R2"]),
        {**_task("T3", "Risks", ["R3"]), "purpose": "List the main risks.", "depends_on": ["T1"],
         "required": False},
        {**_task("T4", "Decisions", ["R4"]), "purpose": "List decisions needed.", "depends_on": ["T2"]},
    )

    merged = merge_to_limit(plan, 3).tasks[-1]

    assert merged.title == "Risks & Decisions"
    assert merged.purpose == "List the main risks. List decisions needed."
    assert merged.covers == ["R3", "R4"]
    assert merged.depends_on == ["T1", "T2"]
    assert merged.required is True


def test_merge_never_joins_a_task_with_its_direct_prerequisite() -> None:
    plan = _plan(
        _task("T1", "Overview", ["R1"]),
        _task("T2", "Phases", ["R2"]),
        {**_task("T3", "Phase details", ["R3"]), "depends_on": ["T2"]},
    )

    merged = merge_to_limit(plan, 2)

    assert [(task.id, task.covers, task.depends_on) for task in merged.tasks] == [
        ("T1", ["R1", "R2"], []),
        ("T3", ["R3"], ["T1"]),
    ]
    assert dependency_order(merged.tasks) is not None


def test_merge_never_joins_tasks_linked_through_another_task() -> None:
    # T2 needs T3 and T3 needs T1, so merging T1 and T2 would create a cycle.
    plan = _plan(
        _task("T1", "Scope", ["R1"]),
        {**_task("T2", "Summary", ["R2"]), "depends_on": ["T3"]},
        _table("T3", "Timeline", ["R3"], depends_on=["T1"]),
    )

    assert merge_to_limit(plan, 2) == plan


def test_plan_fails_clearly_when_no_safe_merge_reaches_the_limit(settings: Settings) -> None:
    alternating = [task if n % 2 else {**task, "tool": "build_table"}
                   for n, task in enumerate(ONE_TASK_PER_ITEM)]
    fake = FakeLLM([{**ANALYSIS, "tasks": alternating}, {"tasks": alternating}])
    client = LLMClient(fake, TokenPacer(settings.pacer_window_tokens, settings.pacer_window_s), settings,
                       sleep=lambda _: None)

    result = AgentPipeline(client, lambda _a, _s: "plan.docx", settings).run(REQUEST)

    assert result.response.status == "failed"
    assert "No safe merge reaches 6 tasks" in result.response.message
    assert len(fake.requests) == 2
