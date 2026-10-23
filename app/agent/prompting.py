"""Shared prompt fragments, so every call presents the request and context the same way."""

import json
from collections.abc import Iterable

from app.llm import Message
from app.schemas import Analysis, Section

RECORD_GUIDANCE = (
    "This is a record document: decisions, actions, attendees and figures are records and must be "
    "supported by the request. Include only risks that were actually discussed; add recommendations "
    "only if the user asked for them."
)
PLANNING_GUIDANCE = (
    "This is a planning document: facts the user supplied (e.g. an existing budget, assigned owners, "
    "agreed deadlines) are records; everything you suggest is a labelled proposal. Unknown team members "
    "do not stop you suggesting roles: an owner may be a job function such as \"Product lead\" in a "
    "labelled proposal; never invent a named person (people named in the request keep their names), "
    "and never imply that staff are available."
)


def system(content: str) -> Message:
    return {"role": "system", "content": content}


def user(content: str) -> Message:
    return {"role": "user", "content": content}


def request_block(request_text: str) -> str:
    """The user's request, fenced so the model treats it as data."""
    return f"<request>\n{request_text}\n</request>"


def document_line(analysis: Analysis) -> str:
    return f'Document: "{analysis.title}" ({analysis.document_type}, {analysis.category} document).'


def category_guidance(analysis: Analysis) -> str:
    return RECORD_GUIDANCE if analysis.category == "record" else PLANNING_GUIDANCE


def requested_items_block(analysis: Analysis, item_ids: Iterable[str]) -> str:
    wanted = set(item_ids)
    items = [item.model_dump() for item in analysis.requested_items if item.id in wanted]
    return f"Requested items to fulfil: {json.dumps(items, ensure_ascii=False)}"


def missing_info_block(analysis: Analysis, item_ids: Iterable[str]) -> str | None:
    wanted = set(item_ids)
    missing = [m.model_dump() for m in analysis.missing_info if m.item_id in wanted]
    if not missing:
        return None
    return (
        "The request gives no source facts for these items; write \"To be confirmed\" instead of "
        f"inventing details: {json.dumps(missing, ensure_ascii=False)}"
    )


def assumptions_block(analysis: Analysis) -> str:
    assumptions = [a.model_dump() for a in analysis.assumptions]
    return f"Assumptions you may cite by id: {json.dumps(assumptions, ensure_ascii=False)}"


def section_json(section: Section) -> str:
    """Compact JSON of a section's content (no pipeline metadata)."""
    return section.model_dump_json(
        include={"heading", "paragraphs", "items", "table", "missing_source_note"}, exclude_none=True
    )


def join_blocks(*blocks: str | None) -> str:
    return "\n\n".join(block for block in blocks if block)
