"""Post-generation guardrails: record-vs-proposal grounding, completeness re-check and the final gate.

Grounding is a conservative substring check against the user's request: a real fact written in a
different format (e.g. another date style) is treated as unsupported and becomes "To be confirmed".
"""

from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from app.guardrails import normalize_text
from app.schemas import Analysis, DocumentCategory, Item, ItemKind, Section

TO_BE_CONFIRMED = "To be confirmed"

_FORCED_RECORD_TYPES = frozenset({"decision", "action", "attendee", "figure"})
_FACT_FIELDS = ("owner", "due", "value")
_EDGE_PUNCTUATION = " .,;:!?\"'()[]"
_FILLER_MARKERS = ("lorem ipsum", "[insert", "[placeholder", "placeholder text", "<to be written>")
_SNIPPET_CHARS = 60


@dataclass
class GroundingResult:
    """Grounded sections plus what changed. `emptied_section_ids` lost all their structured items."""

    sections: list[Section]
    warnings: list[str] = field(default_factory=list)
    emptied_section_ids: set[str] = field(default_factory=set)


@dataclass
class CompletenessResult:
    """Sections kept after the completeness re-check, and the facts the pipeline turns into a status."""

    sections: list[Section]
    warnings: list[str] = field(default_factory=list)
    partial_reasons: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _GroundingContext:
    category: DocumentCategory | None
    source: str
    known_assumptions: frozenset[str]


# --------------------------------------------------------------------------- grounding


def ground_sections(sections: Sequence[Section], analysis: Analysis, request_text: str) -> GroundingResult:
    """Enforce item kinds, then check every record fact against the request."""
    context = _GroundingContext(
        category=analysis.category,
        source=normalize_text(request_text),
        known_assumptions=frozenset(assumption.id for assumption in analysis.assumptions),
    )
    result = GroundingResult(sections=[])

    for section in sections:
        items = _ground_items(section.items, section.heading, context, result.warnings)
        table = section.table
        if table is not None:
            table = table.model_copy(
                update={"rows": _ground_items(table.rows, section.heading, context, result.warnings)}
            )

        had_structured = bool(section.items or (section.table and section.table.rows))
        has_structured = bool(items or (table and table.rows))
        if had_structured and not has_structured:
            result.emptied_section_ids.add(section.task_id)

        result.sections.append(section.model_copy(update={"items": items, "table": table}))
    return result


def _ground_items(
    items: Sequence[Item], heading: str, context: _GroundingContext, warnings: list[str]
) -> list[Item]:
    """Ground each item, dropping removed ones; warnings are appended to `warnings`."""
    grounded = (_ground_item(item, heading, context, warnings) for item in items)
    return [item for item in grounded if item is not None]


def _ground_item(item: Item, heading: str, context: _GroundingContext, warnings: list[str]) -> Item | None:
    """Return the grounded item, or None if it is an unsupported record."""
    item = item.model_copy(update={"kind": _enforced_kind(item, context.category)})

    if item.assumption_id is not None and item.assumption_id not in context.known_assumptions:
        warnings.append(f"'{heading}': cites unknown assumption {item.assumption_id}; citation removed.")
        item = item.model_copy(update={"assumption_id": None})

    if item.kind == "record" and not (
        _supported(item.source_quote, context.source) or _supported(item.text, context.source)
    ):
        warnings.append(f"'{heading}': removed {item.type} not in the request: '{_snippet(item.text)}'.")
        return None

    if item.kind == "record" or context.category == "record":
        updates: dict[str, str] = {}
        for name in _FACT_FIELDS:
            value = getattr(item, name)
            if value and not _is_to_be_confirmed(value) and not _supported(value, context.source):
                updates[name] = TO_BE_CONFIRMED
                warnings.append(f"'{heading}': {name} '{_snippet(value)}' not in the request; marked TBC.")
        item = item.model_copy(update=updates)
    return item


def _enforced_kind(item: Item, category: DocumentCategory | None) -> ItemKind:
    """In record documents, fact-bearing types are always records, whatever the LLM said.

    In planning documents the claimed kind is kept: a claimed record (e.g. a user-assigned task)
    must pass grounding and is never downgraded to a proposal; generated content must be a
    labelled proposal, which the gate checks.
    """
    if category == "record" and item.type in _FORCED_RECORD_TYPES:
        return "record"
    return item.kind


def _supported(value: str | None, source: str) -> bool:
    if not value:
        return False
    needle = normalize_text(value).strip(_EDGE_PUNCTUATION)
    return bool(needle) and needle in source


def _is_to_be_confirmed(value: str) -> bool:
    return normalize_text(value).strip(_EDGE_PUNCTUATION) in {TO_BE_CONFIRMED.casefold(), "tbc"}


def _snippet(text: str) -> str:
    return text if len(text) <= _SNIPPET_CHARS else f"{text[:_SNIPPET_CHARS]}..."


# --------------------------------------------------------------------------- completeness


def check_completeness(
    sections: Sequence[Section], emptied_section_ids: set[str], analysis: Analysis
) -> CompletenessResult:
    """Re-check each section after grounding: keep, mark as missing-source, drop (optional) or fail."""
    missing_ids = {missing.item_id for missing in analysis.missing_info}
    result = CompletenessResult(sections=[])

    for section in sections:
        heading = section.heading
        if is_substantive(section, emptied_section_ids):
            result.sections.append(section)
        elif not section.required:
            result.warnings.append(f"Optional section '{heading}' had no supported content and was removed.")
            result.partial_reasons.append(f"Optional section '{heading}' was removed.")
        elif missing_ids & set(section.covers):
            result.sections.append(_with_missing_source_note(section))
            result.partial_reasons.append(f"'{heading}' had no source facts in the request.")
        else:
            result.failures.append(f"Required section '{heading}' has no supported content.")

    if not any(s.required and s.missing_source_note is None for s in result.sections):
        result.failures.append("No required section kept substantive content, so the request cannot be met.")
    return result


def is_substantive(section: Section, emptied_section_ids: set[str]) -> bool:
    if section.task_id in emptied_section_ids:
        return False
    if section.table is not None:
        return bool(section.table.rows)
    return bool(section.paragraphs or section.items)


def _with_missing_source_note(section: Section) -> Section:
    note = f"No information for '{section.heading}' was provided in the request — to be confirmed."
    return section.model_copy(
        update={"paragraphs": [], "items": [], "table": None, "missing_source_note": note}
    )


# --------------------------------------------------------------------------- duplicates


def remove_duplicate_records(sections: Sequence[Section]) -> tuple[list[Section], list[str]]:
    """Keep a record repeated in several sections only where its type makes up the largest share.

    E.g. a decision listed under both "Meeting Details" and "Decisions" stays under "Decisions".
    Two records match when they share a type and a source quote, or one text contains the other.
    Ties keep both copies, and a section never loses all of its structured items this way.
    """
    shares = [_type_shares(section) for section in sections]
    result: list[Section] = []
    warnings: list[str] = []
    for index, section in enumerate(sections):
        items = [i for i in section.items if not _belongs_elsewhere(i, index, sections, shares)]
        rows = [r for r in _rows(section) if not _belongs_elsewhere(r, index, sections, shares)]
        removed = len(section.items) + len(_rows(section)) - len(items) - len(rows)
        if removed == 0 or not (items or rows):
            result.append(section)
            continue
        table = section.table.model_copy(update={"rows": rows}) if section.table else None
        result.append(section.model_copy(update={"items": items, "table": table}))
        warnings.append(f"'{section.heading}': removed {removed} item(s) repeated in another section.")
    return result, warnings


def _belongs_elsewhere(
    item: Item, index: int, sections: Sequence[Section], shares: list[dict[str, float]]
) -> bool:
    if item.kind != "record":
        return False
    own_share = shares[index][item.type]
    return any(
        shares[other].get(item.type, 0.0) > own_share
        and any(_same_record(item, candidate) for candidate in _all_items(sections[other]))
        for other in range(len(sections))
        if other != index
    )


def _type_shares(section: Section) -> dict[str, float]:
    items = list(_all_items(section))
    counts = Counter(item.type for item in items)
    return {item_type: count / len(items) for item_type, count in counts.items()}


def _same_record(first: Item, second: Item) -> bool:
    if first.type != second.type or second.kind != "record":
        return False
    if first.source_quote and second.source_quote:
        if _match_key(first.source_quote) == _match_key(second.source_quote):
            return True
    first_text, second_text = _match_key(first.text), _match_key(second.text)
    return bool(first_text and second_text) and (first_text in second_text or second_text in first_text)


def _match_key(text: str) -> str:
    return normalize_text(text).strip(_EDGE_PUNCTUATION)


def _rows(section: Section) -> list[Item]:
    return section.table.rows if section.table is not None else []


# --------------------------------------------------------------------------- final gate


def check_gate(sections: Sequence[Section], analysis: Analysis) -> list[str]:
    """Final checks before rendering; any failure means no document is delivered."""
    failures: list[str] = []
    covered = {item_id for section in sections for item_id in section.covers}
    covered |= analysis.covered_by_assumptions()
    for requested in analysis.requested_items:
        if requested.id not in covered:
            failures.append(
                f"Requested item {requested.id} ('{_snippet(requested.description)}') is not covered."
            )

    for section in sections:
        for item in _all_items(section):
            # An assumption ID explains a proposal's basis; it does not replace the visible label.
            if item.kind == "proposal" and item.label is None:
                failures.append(
                    f"'{section.heading}': proposal '{_snippet(item.text)}' has no label "
                    "(Proposed, Recommended, Potential or Illustrative)."
                )
        if section.required and _contains_filler(section):
            failures.append(f"Required section '{section.heading}' contains placeholder text.")
    return failures


def _all_items(section: Section) -> Iterator[Item]:
    yield from section.items
    if section.table is not None:
        yield from section.table.rows


def _contains_filler(section: Section) -> bool:
    texts = [*section.paragraphs, *(item.text for item in _all_items(section))]
    return any(marker in normalize_text(text) for text in texts for marker in _FILLER_MARKERS)
