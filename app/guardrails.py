"""Pre-generation guardrails: request screening and plan validation.

These are limited safeguards, not guarantees: the keyword screens catch obvious attempts only.
"""

import re
import unicodedata
from collections.abc import Collection, Iterable, Sequence

from app.config import Settings
from app.schemas import Analysis, Plan, Task

_INJECTION_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|system|your)\b"
        r".{0,20}\b(instructions?|prompts?|rules|guidelines)\b",
        r"\b(reveal|show|print|repeat|leak)\b.{0,30}"
        r"\b(system|hidden|initial)\s+(prompt|instructions?|message)\b",
        r"\byou are now\b.{0,40}\b(unrestricted|jailbroken|dan)\b",
        r"\b(jailbreak|developer mode|do anything now)\b",
        r"\bpretend\b.{0,30}\b(no|without)\b.{0,20}\b(rules|restrictions|guidelines|filters)\b",
        r"<\|?(im_start|im_end|system|endoftext)\|?>",
    )
)

_UNSAFE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\b(make|build|assemble|synthesi[sz]e|manufacture)\b.{0,30}"
        r"\b(bombs?|explosives?|nerve agents?|bioweapons?|chemical weapons?)\b",
        r"\b(write|create|develop|build)\b.{0,30}\b(ransomware|malware|keyloggers?|computer virus)\b",
        r"\b(child|minor)\b.{0,20}\b(sexual|porn)",
        r"\bhow to\b.{0,20}\b(kill|murder|poison)\b.{0,20}\b(someone|a person|people)\b",
    )
)


# A heading that is only an assumptions section, e.g. "Assumptions" or "Planning Assumptions".
_ASSUMPTIONS_TITLE = re.compile(r"(?:[a-z]+ )?assumptions?")


class InputRejected(Exception):
    """The request was refused by a keyword screen before any LLM call."""

    def __init__(self, category: str, reason: str) -> None:
        super().__init__(reason)
        self.category = category
        self.reason = reason


def normalize_text(text: str) -> str:
    """Unicode-normalise, case-fold and collapse whitespace, for matching only."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def screen_request(text: str) -> None:
    """Raise InputRejected if the request matches a prompt-injection or unsafe-content pattern."""
    normalized = normalize_text(text)
    if any(pattern.search(normalized) for pattern in _INJECTION_PATTERNS):
        raise InputRejected(
            "prompt_injection", "The request contains instructions that try to override the agent's rules."
        )
    if any(pattern.search(normalized) for pattern in _UNSAFE_PATTERNS):
        raise InputRejected("unsafe_content", "The request asks for clearly unsafe content.")


def dependency_order(tasks: Sequence[Task]) -> list[Task] | None:
    """Return tasks so each follows its dependencies, staying as close to plan order as possible.

    Returns None if the dependencies cannot be satisfied (a cycle or a missing task).
    """
    ordered: list[Task] = []
    done: set[str] = set()
    remaining = list(tasks)
    while remaining:
        ready = next((task for task in remaining if set(task.depends_on) <= done), None)
        if ready is None:
            return None
        ordered.append(ready)
        done.add(ready.id)
        remaining.remove(ready)
    return ordered


def validate_plan(
    plan: Plan, analysis: Analysis, known_tools: Collection[str], settings: Settings
) -> list[str]:
    """Return every problem with the plan, phrased so it can be fed back for one re-plan."""
    tasks = plan.tasks
    errors: list[str] = []

    if not settings.min_plan_tasks <= len(tasks) <= settings.max_plan_tasks:
        errors.append(
            f"The plan must have {settings.min_plan_tasks}-{settings.max_plan_tasks} tasks; "
            f"it has {len(tasks)}."
        )
    if len(tasks) < settings.min_plan_tasks:
        # Seen live: the model planned content + "Assumptions"; folding the latter left one task.
        errors.append(
            "Split the content into more sections; an Assumptions-only section does not count, "
            "because it is added automatically."
        )

    ids = [task.id for task in tasks]
    duplicates = sorted({task_id for task_id in ids if ids.count(task_id) > 1})
    if duplicates:
        errors.append(f"Task ids must be unique; duplicated: {', '.join(duplicates)}.")

    task_ids = set(ids)
    requested_ids = {item.id for item in analysis.requested_items}
    dependencies_valid = not duplicates
    for task in tasks:
        if task.tool not in known_tools:
            errors.append(
                f"{task.id}: unknown tool '{task.tool}'. Use one of: {', '.join(sorted(known_tools))}."
            )
        missing = [dep for dep in task.depends_on if dep not in task_ids]
        if missing:
            dependencies_valid = False
            errors.append(f"{task.id}: depends on tasks that do not exist: {', '.join(missing)}.")
        unknown_items = [item_id for item_id in task.covers if item_id not in requested_ids]
        if unknown_items:
            errors.append(f"{task.id}: covers unknown requested items: {', '.join(unknown_items)}.")

    if dependencies_valid and dependency_order(tasks) is None:
        errors.append("Task dependencies contain a cycle.")

    covered = {item_id for task in tasks for item_id in task.covers} | analysis.covered_by_assumptions()
    uncovered = sorted(requested_ids - covered)
    if uncovered:
        errors.append(f"Requested items not covered by any task: {', '.join(uncovered)}.")
    return errors


def fold_assumptions_tasks(plan: Plan, analysis: Analysis) -> tuple[Plan, Analysis]:
    """Remove planned tasks that are only an Assumptions section; the document always ends with one.

    Their covered items are credited to the automatic section via `assumptions_cover`. Nothing changes
    when the analysis lists no assumptions (validation then reports any uncovered item), and titles that
    mix in other content (e.g. "Risks and Assumptions") are kept so no content is lost.
    """
    folded = [task for task in plan.tasks if _ASSUMPTIONS_TITLE.fullmatch(normalize_text(task.title))]
    if not folded or not analysis.assumptions:
        return plan, analysis
    folded_ids = {task.id for task in folded}
    tasks = [
        task.model_copy(update={"depends_on": [dep for dep in task.depends_on if dep not in folded_ids]})
        for task in plan.tasks
        if task.id not in folded_ids
    ]
    cover = _unique([*analysis.assumptions_cover, *(item_id for task in folded for item_id in task.covers)])
    return Plan(tasks=tasks), analysis.model_copy(update={"assumptions_cover": cover})


def merge_to_limit(plan: Plan, max_tasks: int) -> Plan:
    """Merge neighbouring tasks until the plan has at most `max_tasks` tasks, where it is safe.

    A pair is safe to merge only if both tasks use the same tool and neither depends on the other,
    directly or transitively, so merging can never create a self-dependency or a cycle. Pairs are taken
    from the end so the opening sections keep their own headings. A merged task keeps both purposes, all
    covers and all prerequisites. Stops early if no safe pair is left; the caller reports that.
    """
    tasks = list(plan.tasks)
    while len(tasks) > max_tasks:
        index = next((i for i in range(len(tasks) - 2, -1, -1) if _can_merge(tasks, i)), None)
        if index is None:
            break
        first, second = tasks[index], tasks[index + 1]
        merged = Task(
            id=first.id,
            tool=first.tool,
            title=f"{first.title} & {second.title}",
            purpose=f"{first.purpose} {second.purpose}",
            depends_on=_unique([*first.depends_on, *second.depends_on]),
            covers=_unique([*first.covers, *second.covers]),
            required=first.required or second.required,
        )
        tasks[index:index + 2] = [merged]
        tasks = [_redirect_dependency(task, second.id, first.id) for task in tasks]
    return Plan(tasks=tasks)


def _can_merge(tasks: Sequence[Task], index: int) -> bool:
    first, second = tasks[index], tasks[index + 1]
    return (
        first.tool == second.tool
        and not _depends_on(tasks, second.id, first.id)
        and not _depends_on(tasks, first.id, second.id)
    )


def _depends_on(tasks: Sequence[Task], task_id: str, other_id: str) -> bool:
    """True if `task_id` needs `other_id`, directly or through other tasks."""
    by_id = {task.id: task for task in tasks}
    pending, seen = [task_id], {task_id}
    while pending:
        current = by_id.get(pending.pop())
        for dep in current.depends_on if current else ():
            if dep == other_id:
                return True
            if dep not in seen:
                seen.add(dep)
                pending.append(dep)
    return False


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _redirect_dependency(task: Task, old_id: str, new_id: str) -> Task:
    if old_id not in task.depends_on:
        return task
    return task.model_copy(
        update={"depends_on": _unique(new_id if dep == old_id else dep for dep in task.depends_on)}
    )


def enforce_required(plan: Plan, analysis: Analysis) -> Plan:
    """Set `required` from covers, whatever the LLM said: a task is required iff it covers a requested item.

    Sections the user did not ask for are optional, so their failure can make a run partial but never
    discard the requested content.
    """
    requested_ids = {item.id for item in analysis.requested_items}
    return Plan(
        tasks=[
            task.model_copy(update={"required": bool(requested_ids & set(task.covers))})
            for task in plan.tasks
        ]
    )
