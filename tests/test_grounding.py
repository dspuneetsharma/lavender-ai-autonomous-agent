"""Grounding and the proposal-label gate (plan group 6).

Planning documents: user-supplied records stay grounded, generated content stays a labelled proposal.
Record documents (minutes): unsupported facts become "To be confirmed" and unsupported decisions are removed.
"""

import pytest

from app.docx_builder import build_document
from app.grounding import TO_BE_CONFIRMED, check_gate, ground_sections
from app.schemas import Analysis, Assumption, ColumnLabels, Item, Section, Table

PLANNING_REQUEST = (
    "Draft a project plan for our CRM migration. Priya owns the data migration, due March 31. "
    "Budget is $40,000."
)


@pytest.fixture
def planning_analysis() -> Analysis:
    return Analysis(
        is_document_request=True,
        title="CRM Migration Plan",
        document_type="project plan",
        category="planning",
        requested_items=[{"id": "R1", "description": "Workstreams and owners", "quote": "project plan"}],
    )


def _ground_one(item: Item, analysis: Analysis) -> tuple[list[Item], list[str]]:
    section = Section(
        task_id="T1",
        heading="Workstreams",
        covers=["R1"],
        table=Table(columns=ColumnLabels(text="Task", owner="Owner", due="Due"), rows=[item]),
    )
    result = ground_sections([section], analysis, PLANNING_REQUEST)
    return result.sections[0].table.rows, result.warnings


def test_supported_assignment_stays_a_grounded_record(planning_analysis: Analysis) -> None:
    item = Item(
        type="task", kind="record", text="Data migration",
        source_quote="Priya owns the data migration", owner="Priya", due="March 31",
    )

    rows, warnings = _ground_one(item, planning_analysis)

    assert len(rows) == 1
    assert rows[0].kind == "record"
    assert (rows[0].owner, rows[0].due) == ("Priya", "March 31")
    assert warnings == []


def test_invented_deadline_on_valid_quote_is_marked_to_be_confirmed(planning_analysis: Analysis) -> None:
    item = Item(
        type="task", kind="record", text="Data migration",
        source_quote="Priya owns the data migration", owner="Priya", due="April 15",
    )

    rows, warnings = _ground_one(item, planning_analysis)

    assert len(rows) == 1
    assert rows[0].kind == "record"
    assert rows[0].owner == "Priya"
    assert rows[0].due == TO_BE_CONFIRMED
    assert len(warnings) == 1 and "due 'April 15'" in warnings[0]


def test_unsupported_claimed_assignment_is_removed_not_downgraded(planning_analysis: Analysis) -> None:
    item = Item(
        type="task", kind="record", text="Security audit",
        source_quote="Omar owns the security audit", owner="Omar", due="May 1",
    )

    rows, warnings = _ground_one(item, planning_analysis)

    assert rows == []
    assert len(warnings) == 1 and "removed task" in warnings[0]


def test_generated_proposal_is_kept_unchanged_and_passes_the_gate(planning_analysis: Analysis) -> None:
    item = Item(
        type="task", kind="proposal", text="Train the support team",
        owner="Support lead", due="Week 6", label="Proposed",
    )

    rows, warnings = _ground_one(item, planning_analysis)

    assert len(rows) == 1
    assert rows[0].kind == "proposal"
    assert (rows[0].owner, rows[0].due, rows[0].label) == ("Support lead", "Week 6", "Proposed")
    assert warnings == []
    section = Section(
        task_id="T1", heading="Workstreams", covers=["R1"],
        table=Table(columns=ColumnLabels(text="Task", owner="Owner", due="Due"), rows=rows),
    )
    assert check_gate([section], planning_analysis) == []


def test_potential_risk_is_kept_and_rendered_with_its_label(planning_analysis: Analysis) -> None:
    risk = Item(
        type="point", kind="proposal", label="Potential",
        text="Customer records could be lost during migration; run a full trial migration first.",
    )
    section = Section(
        task_id="T1", heading="Potential Risks & Suggested Mitigations", covers=["R1"], items=[risk]
    )

    grounded = ground_sections([section], planning_analysis, PLANNING_REQUEST)

    assert grounded.warnings == []
    assert grounded.sections[0].items == [risk]
    assert check_gate(grounded.sections, planning_analysis) == []
    rendered = [p.text for p in build_document(planning_analysis, grounded.sections).paragraphs]
    assert f"Potential: {risk.text}" in rendered


@pytest.fixture
def analysis_with_assumption(planning_analysis: Analysis) -> Analysis:
    assumption = Assumption(id="A1", statement="Internal staff only", rationale="No extra budget")
    return planning_analysis.model_copy(update={"assumptions": [assumption]})


def _gate_one(item: Item, analysis: Analysis) -> list[str]:
    return check_gate([Section(task_id="T1", heading="Phases", covers=["R1"], items=[item])], analysis)


def test_gate_rejects_a_proposal_with_an_assumption_id_but_no_label(
    analysis_with_assumption: Analysis,
) -> None:
    item = Item(type="task", kind="proposal", text="Run a two-day discovery", assumption_id="A1")

    failures = _gate_one(item, analysis_with_assumption)

    assert len(failures) == 1 and "has no label" in failures[0]


@pytest.mark.parametrize("assumption_id", ["A1", None])
def test_gate_accepts_a_labelled_proposal_with_or_without_an_assumption_id(
    analysis_with_assumption: Analysis, assumption_id: str | None
) -> None:
    item = Item(type="task", kind="proposal", text="Run a two-day discovery", label="Proposed",
                assumption_id=assumption_id)

    assert _gate_one(item, analysis_with_assumption) == []


def test_gate_does_not_require_a_label_on_records(analysis_with_assumption: Analysis) -> None:
    item = Item(
        type="task", kind="record", text="Data migration", source_quote="Priya owns the data migration"
    )

    assert _gate_one(item, analysis_with_assumption) == []


# --------------------------------------------------------------------------- meeting minutes (record)

MINUTES_REQUEST = (
    "Minutes for Monday product sync. Attendees: Priya, Omar. We decided to delay the launch to March 3. "
    "Priya will update the launch checklist by Friday."
)


@pytest.fixture
def minutes_analysis() -> Analysis:
    return Analysis(
        is_document_request=True,
        title="Product Sync Minutes",
        document_type="meeting minutes",
        category="record",
        requested_items=[{"id": "R1", "description": "Action items", "quote": "action items"}],
    )


def test_minutes_unsupported_owner_and_deadline_become_to_be_confirmed(minutes_analysis: Analysis) -> None:
    action = Item(type="action", kind="record", text="Update the launch checklist",
                  source_quote="update the launch checklist", owner="Sam", due="March 10")
    section = Section(task_id="T1", heading="Action Items", covers=["R1"], items=[action])

    grounded = ground_sections([section], minutes_analysis, MINUTES_REQUEST)

    item = grounded.sections[0].items[0]
    assert (item.owner, item.due) == (TO_BE_CONFIRMED, TO_BE_CONFIRMED)
    assert len(grounded.warnings) == 2


def test_minutes_supported_action_is_kept_as_a_record_even_if_called_a_proposal(
    minutes_analysis: Analysis,
) -> None:
    action = Item(type="action", kind="proposal", text="Update the launch checklist",
                  source_quote="Priya will update the launch checklist", owner="Priya", due="Friday")
    section = Section(task_id="T1", heading="Action Items", covers=["R1"], items=[action])

    grounded = ground_sections([section], minutes_analysis, MINUTES_REQUEST)

    item = grounded.sections[0].items[0]
    assert (item.kind, item.owner, item.due) == ("record", "Priya", "Friday")
    assert grounded.warnings == []


def test_minutes_unsupported_decision_is_removed(minutes_analysis: Analysis) -> None:
    decisions = [
        Item(type="decision", kind="record", text="Delay the launch",
             source_quote="decided to delay the launch to March 3"),
        Item(type="decision", kind="record", text="Hire two engineers"),
    ]
    section = Section(task_id="T1", heading="Decisions", covers=["R1"], items=decisions)

    grounded = ground_sections([section], minutes_analysis, MINUTES_REQUEST)

    assert [item.text for item in grounded.sections[0].items] == ["Delay the launch"]
    assert grounded.warnings == ["'Decisions': removed decision not in the request: 'Hire two engineers'."]


DAY_PLAN_REQUEST = "give me a doc of my day plan 3 am walk 8am to 12 pm metting then from 7 game"


def test_day_plan_times_written_differently_are_kept(planning_analysis: Analysis) -> None:
    # Live failure: "8am-12pm" and "7 pm" became "To be confirmed" because the request says "8am to 12 pm"
    # and "from 7", so the review flagged the meeting time as missing and the run failed.
    rows = [
        Item(type="task", kind="record", text="Walk", source_quote="3 am walk", due="3:00 AM"),
        Item(type="task", kind="record", text="Meeting", source_quote="8am to 12 pm metting", due="8am-12pm"),
        Item(type="task", kind="record", text="Game", source_quote="from 7 game", due="7 pm"),
    ]
    section = Section(task_id="T1", heading="Daily Schedule", covers=["R1"],
                      table=Table(columns=ColumnLabels(text="Activity", due="Time"), rows=rows))

    grounded = ground_sections([section], planning_analysis, DAY_PLAN_REQUEST)

    assert [row.due for row in grounded.sections[0].table.rows] == ["3:00 AM", "8am-12pm", "7 pm"]
    assert grounded.warnings == []


def test_day_plan_invented_time_is_still_marked_to_be_confirmed(planning_analysis: Analysis) -> None:
    item = Item(type="task", kind="record", text="Meeting", source_quote="8am to 12 pm metting", due="9am-1pm")
    section = Section(task_id="T1", heading="Daily Schedule", covers=["R1"], items=[item])

    grounded = ground_sections([section], planning_analysis, DAY_PLAN_REQUEST)

    assert grounded.sections[0].items[0].due == TO_BE_CONFIRMED
    assert grounded.warnings == ["'Daily Schedule': due '9am-1pm' not in the request; marked TBC."]


def test_unknown_assumption_id_is_flagged_and_removed(planning_analysis: Analysis) -> None:
    item = Item(type="task", kind="proposal", text="Train the support team", label="Proposed",
                assumption_id="A9")
    section = Section(task_id="T1", heading="Training", covers=["R1"], items=[item])

    grounded = ground_sections([section], planning_analysis, PLANNING_REQUEST)

    assert grounded.sections[0].items[0].assumption_id is None
    assert grounded.warnings == ["'Training': cites unknown assumption A9; citation removed."]


def test_gate_counts_the_automatic_assumptions_section_only_when_assumptions_exist(
    analysis_with_assumption: Analysis,
) -> None:
    covered = analysis_with_assumption.model_copy(update={"assumptions_cover": ["R1"]})
    no_assumptions = covered.model_copy(update={"assumptions": []})

    assert check_gate([], covered) == []
    assert check_gate([], no_assumptions) == ["Requested item R1 ('Workstreams and owners') is not covered."]
