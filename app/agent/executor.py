"""Execute plan tasks in dependency order: one LLM call per task; status comes from the validated result."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from app.agent.prompting import (
    assumptions_block,
    category_guidance,
    document_line,
    join_blocks,
    missing_info_block,
    request_block,
    requested_items_block,
    section_json,
    system,
    user,
)
from app.config import Settings
from app.guardrails import dependency_order
from app.llm import BudgetExhausted, ContextTooLarge, LLMCallFailed, LLMClient, Message, RequestBudget
from app.schemas import Analysis, Plan, Section, Task, TaskStatus
from app.tools import CONTENT_RULES, TOOLS, Tool

logger = logging.getLogger(__name__)

TASK_PROMPT = """You are executing one task of a document plan: write the content of a single section.
{document}
{guidance}

{tool_instructions}

{content_rules}"""


@dataclass
class TaskOutcome:
    """Result of one task. `section` is set only when status is "done"."""

    task: Task
    status: TaskStatus
    detail: str | None = None
    section: Section | None = None


def execute_plan(
    plan: Plan,
    analysis: Analysis,
    request_text: str,
    client: LLMClient,
    budget: RequestBudget,
    settings: Settings,
) -> list[TaskOutcome]:
    """Run every task; return outcomes in plan (document) order. Stops early once a required task fails."""
    ordered = dependency_order(plan.tasks)
    if ordered is None:
        raise ValueError("execute_plan needs a validated, acyclic plan")

    outcomes: dict[str, TaskOutcome] = {}
    stop_reason: str | None = None
    for task in ordered:
        if stop_reason is not None:
            outcome = TaskOutcome(task, "skipped", stop_reason)
        elif blocked := [dep for dep in task.depends_on if outcomes[dep].status != "done"]:
            outcome = TaskOutcome(task, "skipped", f"prerequisite {', '.join(blocked)} not done")
        else:
            prerequisites = [outcomes[dep].section for dep in task.depends_on]
            outcome = _run_task(task, prerequisites, plan, analysis, request_text, client, budget, settings)
        outcomes[task.id] = outcome
        logger.info("Task %s (%s): %s %s", task.id, task.tool, outcome.status, outcome.detail or "")
        if task.required and outcome.status != "done" and stop_reason is None:
            stop_reason = f"not run: required task {task.id} did not complete"
    return [outcomes[task.id] for task in plan.tasks]


def _run_task(
    task: Task,
    prerequisites: Sequence[Section | None],
    plan: Plan,
    analysis: Analysis,
    request_text: str,
    client: LLMClient,
    budget: RequestBudget,
    settings: Settings,
) -> TaskOutcome:
    tool = TOOLS[task.tool]
    sections = [section for section in prerequisites if section is not None]
    messages = _fitting_messages(task, tool, sections, plan, analysis, request_text, client, settings)
    try:
        content = client.chat_json(
            messages,
            tool.output_schema,
            purpose=f"task {task.id}",
            max_completion_tokens=settings.completion_tokens_task,
            budget=budget,
        )
    except BudgetExhausted as exc:
        return TaskOutcome(task, "skipped", exc.reason)
    except (ContextTooLarge, LLMCallFailed) as exc:
        return TaskOutcome(task, "failed", exc.reason)
    return TaskOutcome(task, "done", section=tool.to_section(task, content))


def _fitting_messages(
    task: Task,
    tool: Tool,
    prerequisites: Sequence[Section],
    plan: Plan,
    analysis: Analysis,
    request_text: str,
    client: LLMClient,
    settings: Settings,
) -> list[Message]:
    """Full context if it fits the per-call cap; otherwise drop only the non-essential outline.

    If it still does not fit, the call itself raises ContextTooLarge and the task fails.
    """
    context = (task, tool, prerequisites, plan, analysis, request_text)
    with_outline = task_messages(*context, include_outline=True)
    reserved = client.reservation_tokens(with_outline, tool.output_schema, settings.completion_tokens_task)
    if reserved <= settings.per_call_token_cap:
        return with_outline
    logger.info("Task %s: dropping the document outline to fit the per-call cap", task.id)
    return task_messages(*context, include_outline=False)


def task_messages(
    task: Task,
    tool: Tool,
    prerequisites: Sequence[Section],
    plan: Plan,
    analysis: Analysis,
    request_text: str,
    *,
    include_outline: bool,
) -> list[Message]:
    """Prompt for one task. Request, requested items, missing info, assumptions and prerequisites are
    essential and always included; the outline of other sections is the only optional part."""
    prompt = TASK_PROMPT.format(
        document=document_line(analysis),
        guidance=category_guidance(analysis),
        tool_instructions=tool.instructions,
        content_rules=CONTENT_RULES,
    )
    outline = "Document outline: " + "; ".join(f"{t.id} {t.title}" for t in plan.tasks)
    prerequisite_text = "\n".join(f"{section.task_id}: {section_json(section)}" for section in prerequisites)
    content = join_blocks(
        request_block(request_text),
        f"Task {task.id}: {task.title}\nPurpose: {task.purpose}",
        requested_items_block(analysis, task.covers),
        missing_info_block(analysis, task.covers),
        assumptions_block(analysis),
        outline if include_outline else None,
        f"Content of the sections this task depends on:\n{prerequisite_text}" if prerequisites else None,
    )
    return [system(prompt), user(content)]
