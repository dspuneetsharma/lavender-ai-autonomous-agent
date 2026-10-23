"""Tool registry: the only actions a plan may use. Each has a typed output and a converter to a Section."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.schemas import Section, SectionContent, Table, TableContent, Task

CONTENT_RULES = """Content rules:
- Use only facts from the user's request. Never invent people's names, or present dates, numbers, owners \
or decisions as facts unless the request states them; anything you suggest is a labelled proposal.
- Every item has a kind. "record" = something the request says happened, was decided, assigned or \
supplied (attendees, decisions, actions, owners, deadlines, budgets). "proposal" = something you recommend.
- A record item must copy the supporting words from the request, exactly as written, into source_quote.
- On a record, fill owner, due and value only if the request states them. If a field that matters for \
the item is not stated (e.g. an action's owner or deadline), write "To be confirmed"; leave fields that \
don't apply to the item (e.g. owner or value on a decision) empty.
- Every proposal item must have a label: "Proposed" or "Recommended" ("Potential" for possible risks, \
"Illustrative" for example figures). Set assumption_id only when that specific assumption supports the \
item; otherwise leave it empty. When the user asks for a choice to be labelled as an assumption (e.g. \
a chosen topic or duration), cite that assumption's id on the item. Never word a proposal as already agreed or a potential risk as a \
confirmed problem.
- Paragraphs must not introduce names, dates, numbers or decisions that are not in the request. \
Never invent company-specific facts, statistics or probabilities.
- Write only what belongs under this section's heading; other sections in the outline cover their own \
content. Use prerequisite sections for consistency; never copy their items.
- Never turn the user's instructions (e.g. "Decide how to...") into items, never imply that people or \
teams are available unless the request says so, and refer to people by name or role, not gendered pronouns.
- No placeholder text. Be concise, specific and professional. Do not repeat the section heading."""


@dataclass(frozen=True)
class Tool:
    """A planable action: what the planner sees, what the model must return, and how it becomes a Section."""

    name: str
    description: str
    instructions: str
    output_schema: type[BaseModel]
    to_section: Callable[[Task, Any], Section]  # receives an instance of output_schema


def _section_from_text(task: Task, content: SectionContent) -> Section:
    return Section(
        task_id=task.id,
        heading=task.title,
        paragraphs=content.paragraphs,
        items=content.items,
        required=task.required,
        covers=task.covers,
    )


def _section_from_table(task: Task, content: TableContent) -> Section:
    return Section(
        task_id=task.id,
        heading=task.title,
        paragraphs=[content.intro] if content.intro else [],
        table=Table(columns=content.columns, rows=content.rows),
        required=task.required,
        covers=task.covers,
    )


WRITE_SECTION = Tool(
    name="write_section",
    description="Writes a narrative section: paragraphs plus optional bullet items "
    "(e.g. summary, background, decisions, risks, procedure steps).",
    instructions="Write one document section. Use paragraphs for narrative and items for decisions, "
    "actions, attendees, figures, milestones, tasks, roles or key points.",
    output_schema=SectionContent,
    to_section=_section_from_text,
)

BUILD_TABLE = Tool(
    name="build_table",
    description="Builds a table of items with optional owner, date/time and value columns "
    "(e.g. action items, schedule, timeline, budget, responsibilities).",
    instructions="Build one table. Each row is an item: `text` is the main column and owner, due and "
    "value fill the other columns. In `columns`, give a header label only for the fields you use. "
    "Add at most a one-sentence intro.",
    output_schema=TableContent,
    to_section=_section_from_table,
)

TOOLS: dict[str, Tool] = {tool.name: tool for tool in (WRITE_SECTION, BUILD_TABLE)}


def describe_tools() -> str:
    """One line per tool, for the planner prompt."""
    return "\n".join(f"- {tool.name}: {tool.description}" for tool in TOOLS.values())
