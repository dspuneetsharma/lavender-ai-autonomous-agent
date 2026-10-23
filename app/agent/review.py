"""Mandatory review in bounded chunks, then repair (max N sections), re-grounding and verification."""

import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.agent.prompting import (
    assumptions_block,
    category_guidance,
    document_line,
    join_blocks,
    request_block,
    requested_items_block,
    section_json,
    system,
    user,
)
from app.config import Settings
from app.grounding import ground_sections, is_substantive
from app.llm import LLMClient, LLMError, Message, RequestBudget
from app.schemas import Analysis, Plan, ReviewIssue, ReviewResult, Section, Task
from app.tools import CONTENT_RULES, TOOLS

logger = logging.getLogger(__name__)

REVIEW_PROMPT = """You review sections of a document against the user's request.
{document}
{guidance}

Report only real problems; return an empty issues list when the sections are fine. Issue types:
- unsupported_fact: a name, date, number, owner or decision that is not in the request and is not a \
labelled proposal or a cited assumption, or an item citing an assumption that does not support it.
- missing_requested_item: a requested item the section should fulfil but does not. "To be confirmed" \
does not fulfil an explicit request for suggestions such as suggested roles. A missing requested item \
is always material. Judge it only against the requested items listed for these sections; content in \
another section of the document (listed by heading when present) is not missing.
- unlabelled_proposal: a recommendation worded as already agreed, or a proposal item without a label. \
The valid labels are exactly "Proposed", "Recommended", "Potential" and "Illustrative", on any item type; \
a decision the user still has to make, or a suggested one, correctly carries one of them.
- contradiction: content that conflicts with the request or another section, e.g. different dates, \
order, scope or decisions for the same phase, deliverable or choice.
- quality: unclear, repetitive or unprofessional writing, or content placed under the wrong heading.
Severity is "material" if the issue makes a section wrong or misleading, otherwise "minor". Substantial \
duplication of another section's content, or content under the wrong heading, is material; a brief \
restatement of key decisions in a summary is legitimate and not an issue. \
"To be confirmed" is a valid value for facts only the user can supply, not an issue. Names the request \
gives (e.g. assigned owners) are records, not an issue. Assumptions are made visible by the automatic \
Assumptions section and by items citing an assumption_id (shown as "Assumption A1"); "Assumption" is not \
an item label, so if the user asked for something to be labelled as an assumption, the fix is to cite \
its assumption_id, not to change the label. section_id must \
be the id shown for the section."""

REPAIR_PROMPT = """You are repairing one section of a document after review.
{document}
{guidance}

{tool_instructions}

{content_rules}

Rewrite the section so every listed issue is fixed. Keep everything that is correct."""


class ReviewIncomplete(Exception):
    """Review, repair or verification could not finish; review is mandatory, so the request fails."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class ReviewOutcome:
    """Sections after repair, warnings from minor issues and grounding, and issues still open."""

    sections: list[Section]
    warnings: list[str] = field(default_factory=list)
    unresolved: dict[str, list[str]] = field(default_factory=dict)
    repaired_ids: list[str] = field(default_factory=list)


def review_and_repair(
    sections: Sequence[Section],
    plan: Plan,
    analysis: Analysis,
    request_text: str,
    client: LLMClient,
    budget: RequestBudget,
    settings: Settings,
) -> ReviewOutcome:
    """Review all content sections, repair those with material issues, re-ground and verify them."""
    reviewable = [section for section in sections if section.missing_source_note is None]
    outcome = ReviewOutcome(sections=list(sections))
    headings = [section.heading for section in reviewable]
    material = _review(
        reviewable, headings, analysis, request_text, client, budget, settings, outcome.warnings
    )
    if not material:
        return outcome

    to_repair = list(material)[: settings.max_repair_sections]
    for section_id in list(material)[settings.max_repair_sections:]:
        outcome.unresolved[section_id] = material[section_id]

    tasks = {task.id: task for task in plan.tasks}
    by_id = {section.task_id: section for section in sections}
    repaired = [
        _repair(by_id[sid], material[sid], tasks, analysis, request_text, client, budget, settings)
        for sid in to_repair
    ]
    grounded = ground_sections(repaired, analysis, request_text)
    outcome.warnings.extend(grounded.warnings)

    still_open = _review(
        grounded.sections, headings, analysis, request_text, client, budget, settings, outcome.warnings
    )
    for section in grounded.sections:
        if not is_substantive(section, grounded.emptied_section_ids):
            outcome.unresolved[section.task_id] = ["repair left no supported content"]
        elif section.task_id in still_open:
            outcome.unresolved[section.task_id] = still_open[section.task_id]
        by_id[section.task_id] = section

    outcome.repaired_ids = to_repair
    outcome.sections = [by_id[section.task_id] for section in sections]
    return outcome


def _review(
    sections: Sequence[Section],
    document_headings: Sequence[str],
    analysis: Analysis,
    request_text: str,
    client: LLMClient,
    budget: RequestBudget,
    settings: Settings,
    warnings: list[str],
) -> dict[str, list[str]]:
    """Review in chunks; minor issues go to `warnings`, material ones are returned per section id.

    `document_headings` lists every reviewed section, so a chunk knows what the rest of the document holds.
    """
    material: dict[str, list[str]] = defaultdict(list)
    headings = {section.task_id: section.heading for section in sections}
    for chunk in _chunks(sections, document_headings, analysis, request_text, client, settings):
        messages = review_messages(chunk, analysis, request_text, document_headings)
        for issue in _review_chunk(chunk, messages, client, budget, settings):
            if issue.section_id not in headings:
                continue
            if issue.severity == "material":
                material[issue.section_id].append(f"{issue.type}: {issue.problem}")
            else:
                warnings.append(f"'{headings[issue.section_id]}': {issue.problem}")
    return dict(material)


def _chunks(
    sections: Sequence[Section],
    document_headings: Sequence[str],
    analysis: Analysis,
    request_text: str,
    client: LLMClient,
    settings: Settings,
) -> list[list[Section]]:
    """Greedily pack sections so every chunk's reservation is within the per-call cap."""

    def fits(chunk: list[Section]) -> bool:
        messages = review_messages(chunk, analysis, request_text, document_headings)
        reserved = client.reservation_tokens(messages, ReviewResult, settings.completion_tokens_task)
        return reserved <= settings.per_call_token_cap

    chunks: list[list[Section]] = []
    current: list[Section] = []
    for section in sections:
        if fits([*current, section]):
            current.append(section)
            continue
        if current:
            chunks.append(current)
        if not fits([section]):
            raise ReviewIncomplete(
                f"Section '{section.heading}' is too large to review within the per-call cap."
            )
        current = [section]
    if current:
        chunks.append(current)
    return chunks


def _review_chunk(
    chunk: Sequence[Section],
    messages: list[Message],
    client: LLMClient,
    budget: RequestBudget,
    settings: Settings,
) -> list[ReviewIssue]:
    purpose = "review " + ",".join(section.task_id for section in chunk)
    try:
        result = client.chat_json(
            messages,
            ReviewResult,
            purpose=purpose,
            max_completion_tokens=settings.completion_tokens_task,
            budget=budget,
        )
    except LLMError as exc:
        raise ReviewIncomplete(f"Review could not complete: {exc.reason}") from exc
    return result.issues


def review_messages(
    chunk: Sequence[Section],
    analysis: Analysis,
    request_text: str,
    document_headings: Sequence[str] = (),
) -> list[Message]:
    """Review prompt: request, covered requested items and assumptions are always included.

    Headings of the document's other sections are listed so content placed there is not reported missing.
    """
    prompt = REVIEW_PROMPT.format(document=document_line(analysis), guidance=category_guidance(analysis))
    covered = {item_id for section in chunk for item_id in section.covers}
    sections_text = "\n".join(f"Section {s.task_id}: {section_json(s)}" for s in chunk)
    shown = {section.heading for section in chunk}
    others = [heading for heading in document_headings if heading not in shown]
    content = join_blocks(
        request_block(request_text),
        requested_items_block(analysis, covered),
        assumptions_block(analysis),
        f"Other sections of the document (not shown): {', '.join(others)}" if others else None,
        f"Sections to review:\n{sections_text}",
    )
    return [system(prompt), user(content)]


def _repair(
    section: Section,
    problems: Sequence[str],
    tasks: dict[str, Task],
    analysis: Analysis,
    request_text: str,
    client: LLMClient,
    budget: RequestBudget,
    settings: Settings,
) -> Section:
    """One rewrite with the review feedback, using the section's original tool."""
    task = tasks[section.task_id]
    tool = TOOLS[task.tool]
    prompt = REPAIR_PROMPT.format(
        document=document_line(analysis),
        guidance=category_guidance(analysis),
        tool_instructions=tool.instructions,
        content_rules=CONTENT_RULES,
    )
    content = join_blocks(
        request_block(request_text),
        requested_items_block(analysis, section.covers),
        assumptions_block(analysis),
        f"Current section {section.task_id}: {section_json(section)}",
        "Issues to fix:\n" + "\n".join(f"- {problem}" for problem in problems),
    )
    try:
        repaired = client.chat_json(
            [system(prompt), user(content)],
            tool.output_schema,
            purpose=f"repair {section.task_id}",
            max_completion_tokens=settings.completion_tokens_task,
            budget=budget,
        )
    except LLMError as exc:
        raise ReviewIncomplete(f"Repair of '{section.heading}' could not complete: {exc.reason}") from exc
    return tool.to_section(task, repaired)
