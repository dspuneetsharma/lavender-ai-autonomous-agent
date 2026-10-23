"""Orchestrates one request end to end and is the only place that decides success / partial / failed."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from app.agent.executor import TaskOutcome, execute_plan
from app.agent.planner import PlanRejected, analyze, finalize_plan
from app.agent.review import ReviewIncomplete, review_and_repair
from app.config import Settings
from app.docx_builder import RenderError
from app.grounding import check_completeness, check_gate, ground_sections, remove_duplicate_records
from app.guardrails import InputRejected, screen_request
from app.llm import LLMClient, LLMError, RequestBudget
from app.schemas import (
    AgentResponse,
    Analysis,
    Plan,
    RunStatus,
    Section,
    TaskReport,
    TaskStatus,
    UsageReport,
)

logger = logging.getLogger(__name__)

HTTP_OK = 200
HTTP_REJECTED = 400
HTTP_NOT_A_DOCUMENT = 422
HTTP_FAILED = 502

CODE_STEPS = (
    ("ground", "Check facts against the request"),
    ("review", "Review sections"),
    ("repair", "Repair and verify flagged sections"),
    ("gate", "Completeness gate"),
    ("render", "Render the Word document"),
)


class Renderer(Protocol):
    """Builds the document and returns its file name; raises RenderError if it cannot."""

    def __call__(self, analysis: Analysis, sections: Sequence[Section]) -> str: ...


@dataclass
class PipelineResult:
    """The API response body, its HTTP status, and the rendered file name (success/partial only)."""

    response: AgentResponse
    http_status: int
    document_name: str | None = None


class _StepFailed(Exception):
    """Internal: stop the run with a failed status."""

    def __init__(self, reason: str, http_status: int = HTTP_FAILED) -> None:
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status


@dataclass
class _Run:
    """Mutable state of one request."""

    budget: RequestBudget
    analysis: Analysis | None = None
    task_reports: list[TaskReport] = field(default_factory=list)
    step_status: dict[str, TaskStatus] = field(default_factory=dict)
    step_detail: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    partial_reasons: list[str] = field(default_factory=list)

    def step(self, step_id: str, status: TaskStatus, detail: str | None = None) -> None:
        self.step_status[step_id] = status
        if detail:
            self.step_detail[step_id] = detail


class AgentPipeline:
    """analyze → plan → execute → ground → completeness → review/repair → gate → render."""

    def __init__(self, client: LLMClient, renderer: Renderer, settings: Settings) -> None:
        self._client = client
        self._renderer = renderer
        self._settings = settings

    def run(self, request_text: str) -> PipelineResult:
        """Process one request. Never raises for expected failures; they become a failed result."""
        run = _Run(budget=RequestBudget.from_settings(self._settings))
        try:
            screen_request(request_text)
        except InputRejected as exc:
            return self._result(run, "failed", exc.reason, HTTP_REJECTED)

        try:
            document_name, sections = self._execute(run, request_text)
        except _StepFailed as exc:
            logger.warning("Request failed: %s", exc.reason)
            return self._result(run, "failed", exc.reason, exc.http_status)

        status: RunStatus = "partial" if run.partial_reasons else "success"
        return self._result(run, status, self._summary(run, sections, status), HTTP_OK, document_name)

    def _execute(self, run: _Run, request_text: str) -> tuple[str, list[Section]]:
        client, budget, settings = self._client, run.budget, self._settings
        try:
            analysis = analyze(request_text, client, budget, settings)
        except LLMError as exc:
            raise _StepFailed(f"Could not analyze the request: {exc.reason}") from exc
        run.analysis = analysis
        if not analysis.is_document_request:
            reason = analysis.refusal_reason or "The request is not asking for a document."
            raise _StepFailed(f"Not a document request: {reason}", HTTP_NOT_A_DOCUMENT)

        try:
            plan, analysis = finalize_plan(analysis, request_text, client, budget, settings)
        except (PlanRejected, LLMError) as exc:
            raise _StepFailed(f"Could not build a valid plan: {exc.reason}") from exc
        run.analysis = analysis

        outcomes = execute_plan(plan, analysis, request_text, client, budget, settings)
        run.task_reports = [_task_report(outcome) for outcome in outcomes]
        sections = self._collect_sections(run, outcomes)

        grounded = ground_sections(sections, analysis, request_text)
        run.warnings.extend(grounded.warnings)
        completeness = check_completeness(grounded.sections, grounded.emptied_section_ids, analysis)
        run.warnings.extend(completeness.warnings)
        run.partial_reasons.extend(completeness.partial_reasons)
        if completeness.failures:
            run.step("ground", "failed", completeness.failures[0])
            raise _StepFailed(" ".join(completeness.failures))
        run.step("ground", "done")

        sections = self._review(run, completeness.sections, plan, analysis, request_text)
        sections, duplicate_warnings = remove_duplicate_records(sections)
        run.warnings.extend(duplicate_warnings)

        gate_failures = check_gate(sections, analysis)
        if gate_failures:
            run.step("gate", "failed", gate_failures[0])
            raise _StepFailed("Completeness gate failed: " + " ".join(gate_failures))
        run.step("gate", "done")

        try:
            document_name = self._renderer(analysis, sections)
        except RenderError as exc:
            logger.warning("Rendering failed: %s", exc)
            run.step("render", "failed", str(exc))
            raise _StepFailed(f"Rendering the document failed: {exc}") from exc
        run.step("render", "done")
        return document_name, sections

    def _collect_sections(self, run: _Run, outcomes: Sequence[TaskOutcome]) -> list[Section]:
        """Apply task-level rules: a required task not done fails the run; optional ones make it partial."""
        required_failures = [o for o in outcomes if o.task.required and o.status != "done"]
        if required_failures:
            first = required_failures[0]
            raise _StepFailed(
                f"Required task {first.task.id} '{first.task.title}' {first.status}: {first.detail}"
            )
        for outcome in outcomes:
            if outcome.status != "done":
                run.partial_reasons.append(f"Optional section '{outcome.task.title}' {outcome.status}.")
                run.warnings.append(f"Optional section '{outcome.task.title}' omitted: {outcome.detail}")
        return [outcome.section for outcome in outcomes if outcome.section is not None]

    def _review(
        self, run: _Run, sections: list[Section], plan: Plan, analysis: Analysis, request_text: str
    ) -> list[Section]:
        """Mandatory review; unresolved material issues fail required sections and drop optional ones."""
        try:
            review = review_and_repair(
                sections, plan, analysis, request_text, self._client, run.budget, self._settings
            )
        except ReviewIncomplete as exc:
            run.step("review", "failed", exc.reason)
            raise _StepFailed(exc.reason) from exc
        run.step("review", "done")
        run.warnings.extend(review.warnings)
        repaired = ", ".join(review.repaired_ids)
        run.step("repair", "done", f"repaired {repaired}" if repaired else "no material issues")

        kept: list[Section] = []
        for section in review.sections:
            problems = review.unresolved.get(section.task_id)
            if not problems:
                kept.append(section)
            elif section.required:
                run.step("repair", "failed", f"'{section.heading}': {problems[0]}")
                raise _StepFailed(f"Unresolved issue in required section '{section.heading}': {problems[0]}")
            else:
                run.partial_reasons.append(f"Optional section '{section.heading}' removed after review.")
                run.warnings.append(f"Optional section '{section.heading}' removed: {problems[0]}")
        return kept

    def _summary(self, run: _Run, sections: Sequence[Section], status: RunStatus) -> str:
        title = run.analysis.title if run.analysis else "document"
        done = sum(1 for report in run.task_reports if report.status == "done")
        planned = len(run.task_reports)
        text = f"Created '{title}' with {len(sections)} sections ({done}/{planned} planned tasks done)."
        if status == "partial":
            text += " Some content is incomplete: " + " ".join(run.partial_reasons)
        return text

    def _result(
        self,
        run: _Run,
        status: RunStatus,
        message: str,
        http_status: int,
        document_name: str | None = None,
    ) -> PipelineResult:
        calls = run.budget.calls
        usage = UsageReport(
            llm_calls=len(calls),
            failed_attempts=sum(1 for call in calls if call.outcome != "ok"),
            tokens_used=run.budget.tokens_used,
            duration_s=round(run.budget.elapsed_s, 2),
        )
        response = AgentResponse(
            status=status,
            message=message,
            title=run.analysis.title if run.analysis else None,
            plan=self._plan_report(run),
            warnings=list(dict.fromkeys(run.warnings)),
            usage=usage,
        )
        return PipelineResult(response=response, http_status=http_status, document_name=document_name)

    def _plan_report(self, run: _Run) -> list[TaskReport]:
        """LLM-planned tasks followed by the code-appended steps; steps never reached show as skipped."""
        if not run.task_reports:
            return []
        steps = [
            TaskReport(
                id=step_id,
                title=title,
                tool="code",
                required=True,
                status=run.step_status.get(step_id, "skipped"),
                detail=run.step_detail.get(step_id),
            )
            for step_id, title in CODE_STEPS
        ]
        return [*run.task_reports, *steps]


def _task_report(outcome: TaskOutcome) -> TaskReport:
    task = outcome.task
    return TaskReport(
        id=task.id,
        title=task.title,
        tool=task.tool,
        required=task.required,
        status=outcome.status,
        detail=outcome.detail,
    )
