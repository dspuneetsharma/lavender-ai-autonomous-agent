"""Completeness re-check after grounding (plan group 7)."""

import pytest

from app.grounding import check_completeness, ground_sections
from app.schemas import Analysis, Item, Section

REQUEST = "Minutes for Monday product sync. We decided to delay the launch to March 3."


@pytest.fixture
def analysis() -> Analysis:
    return Analysis(
        is_document_request=True,
        title="Product Sync Minutes",
        document_type="meeting minutes",
        category="record",
        requested_items=[
            {"id": "R1", "description": "Action items", "quote": "action items"},
            {"id": "R2", "description": "Summary", "quote": "summary"},
        ],
        missing_info=[{"item_id": "R1", "detail": "No actions or owners were given"}],
    )


def _sections() -> list[Section]:
    summary = Section(task_id="T1", heading="Summary", covers=["R2"], paragraphs=["The team met."])
    actions = Section(task_id="T2", heading="Action Items", covers=["R1"], paragraphs=["Agreed actions:"],
                      items=[Item(type="action", kind="record", text="Invented action")])
    return [summary, actions]


def test_required_section_emptied_with_missing_info_gets_a_note_and_makes_the_run_partial(
    analysis: Analysis,
) -> None:
    grounded = ground_sections(_sections(), analysis, REQUEST)

    result = check_completeness(grounded.sections, grounded.emptied_section_ids, analysis)

    assert result.failures == []
    assert result.partial_reasons == ["'Action Items' had no source facts in the request."]
    assert "to be confirmed" in result.sections[1].missing_source_note
    assert result.sections[1].items == [] and result.sections[1].paragraphs == []


def test_required_section_emptied_without_missing_info_fails(analysis: Analysis) -> None:
    no_missing = analysis.model_copy(update={"missing_info": []})
    grounded = ground_sections(_sections(), no_missing, REQUEST)

    result = check_completeness(grounded.sections, grounded.emptied_section_ids, no_missing)

    assert result.failures == ["Required section 'Action Items' has no supported content."]


def test_no_substantive_required_content_fails(analysis: Analysis) -> None:
    grounded = ground_sections(_sections()[1:], analysis, REQUEST)

    result = check_completeness(grounded.sections, grounded.emptied_section_ids, analysis)

    assert result.failures == ["No required section kept substantive content, so the request cannot be met."]


def test_optional_section_with_no_supported_content_is_dropped_as_partial(analysis: Analysis) -> None:
    summary, actions = _sections()
    optional = actions.model_copy(update={"required": False, "covers": []})
    grounded = ground_sections([summary, optional], analysis, REQUEST)

    result = check_completeness(grounded.sections, grounded.emptied_section_ids, analysis)

    assert [section.heading for section in result.sections] == ["Summary"]
    assert result.partial_reasons == ["Optional section 'Action Items' was removed."]
    assert result.failures == []
