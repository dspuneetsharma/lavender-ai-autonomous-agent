"""Records repeated across sections stay only where their type belongs (seen live in minutes)."""

from app.grounding import remove_duplicate_records
from app.schemas import ColumnLabels, Item, Section, Table


def _record(item_type: str, text: str) -> Item:
    return Item(type=item_type, kind="record", text=text, source_quote=text)


DECISION = _record("decision", "the Q4 discount offer will end on 30 November 2026")
ACTION = _record("action", "Arjun will send the updated client list to Meera")
ATTENDEE = _record("attendee", "Priya (Sales Manager)")


def test_minutes_details_keep_attendees_and_lose_repeated_decisions_and_actions() -> None:
    # The details section restates a decision without its leading "the"; it still matches.
    restated = Item(type="decision", kind="record", text="Q4 discount offer will end on 30 November 2026")
    details = Section(task_id="T1", heading="Meeting Details", items=[ATTENDEE, restated, ACTION])
    decisions = Section(task_id="T2", heading="Decisions", items=[DECISION])
    actions = Section(task_id="T3", heading="Action Items",
                      table=Table(columns=ColumnLabels(text="Action"), rows=[ACTION]))

    sections, warnings = remove_duplicate_records([details, decisions, actions])

    assert sections[0].items == [ATTENDEE]
    assert sections[1] == decisions and sections[2] == actions
    assert warnings == ["'Meeting Details': removed 2 item(s) repeated in another section."]


def test_equal_shares_keep_both_copies() -> None:
    first = Section(task_id="T1", heading="Summary", items=[DECISION])
    second = Section(task_id="T2", heading="Decisions", items=[DECISION])

    sections, warnings = remove_duplicate_records([first, second])

    assert sections == [first, second] and warnings == []


def test_a_section_is_never_emptied() -> None:
    mixed = Section(task_id="T1", heading="Highlights", items=[DECISION, ACTION])
    decisions = Section(task_id="T2", heading="Decisions", items=[DECISION])
    actions = Section(task_id="T3", heading="Actions", items=[ACTION])

    sections, _ = remove_duplicate_records([mixed, decisions, actions])

    assert sections[0] == mixed


def test_proposals_are_never_removed() -> None:
    proposal = Item(type="task", kind="proposal", text="Run a pilot survey", label="Proposed")
    other = _record("attendee", "Raghav")
    plan = Section(task_id="T1", heading="Plan", items=[proposal, other])
    tasks = Section(task_id="T2", heading="Tasks", items=[proposal])

    sections, warnings = remove_duplicate_records([plan, tasks])

    assert sections[0] == plan and warnings == []
