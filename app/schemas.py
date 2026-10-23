"""Typed data contracts: API bodies, LLM outputs (analysis, plan, tool content, review) and document sections.

LLM-facing models carry short field descriptions because their JSON Schema is sent to the model.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from app.config import get_settings

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
RequestedItemId = Annotated[str, StringConstraints(pattern=r"^R\d+$")]
AssumptionId = Annotated[str, StringConstraints(pattern=r"^A\d+$")]
TaskId = Annotated[str, StringConstraints(pattern=r"^T\d+$")]

DocumentCategory = Literal["record", "planning"]
ItemKind = Literal["record", "proposal"]
ItemType = Literal["decision", "action", "attendee", "figure", "milestone", "task", "role", "point"]
ProposalLabel = Literal["Proposed", "Recommended", "Potential", "Illustrative"]
Severity = Literal["material", "minor"]
IssueType = Literal[
    "unsupported_fact", "missing_requested_item", "unlabelled_proposal", "contradiction", "quality"
]
TaskStatus = Literal["done", "failed", "skipped"]
RunStatus = Literal["success", "partial", "failed"]


# --------------------------------------------------------------------------- analysis and plan


class LLMModel(BaseModel):
    """Base for models the LLM must produce: unknown fields are rejected, so a wrong shape is retried."""

    model_config = ConfigDict(extra="forbid")



class RequestedItem(LLMModel):
    """Something the user explicitly asked for."""

    id: RequestedItemId
    description: NonEmptyStr
    quote: NonEmptyStr = Field(description="Short verbatim quote from the request")


class Assumption(LLMModel):
    """A planning choice the agent made where the request was silent."""

    id: AssumptionId
    statement: NonEmptyStr
    rationale: NonEmptyStr


class MissingInfo(LLMModel):
    """A requested item for which the request supplies no source facts."""

    item_id: RequestedItemId
    detail: NonEmptyStr


class Task(LLMModel):
    """One step of the plan; executed by exactly one tool call."""

    id: TaskId
    tool: NonEmptyStr = Field(description="Tool name from the tool list")
    title: NonEmptyStr = Field(description="Section heading this task produces")
    purpose: NonEmptyStr
    depends_on: list[TaskId] = Field(
        default_factory=list, description="Tasks whose output this task needs"
    )
    covers: list[RequestedItemId] = Field(
        default_factory=list, description="Requested items this task fulfils"
    )
    required: bool = True


class Plan(LLMModel):
    """The ordered task list."""

    tasks: list[Task]


class Analysis(LLMModel):
    """Output of the analyze call: what was asked, what is known, and the plan."""

    is_document_request: bool
    refusal_reason: str | None = Field(None, description="Set only when is_document_request is false")
    title: str | None = None
    document_type: str | None = Field(None, description="e.g. 'meeting minutes', 'project plan'")
    category: DocumentCategory | None = Field(
        None, description="record = documents what happened; planning = proposes what should happen"
    )
    requested_items: list[RequestedItem] = Field(default_factory=list)
    facts: list[NonEmptyStr] = Field(
        default_factory=list, description="Facts stated in the request, verbatim"
    )
    missing_info: list[MissingInfo] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    assumptions_cover: list[RequestedItemId] = Field(
        default_factory=list,
        description="Requested items fulfilled by the automatic Assumptions section (needs assumptions)",
    )
    tasks: list[Task] = Field(default_factory=list)

    @model_validator(mode="after")
    def _consistent(self) -> "Analysis":
        """A document request needs its descriptors; missing_info and assumptions_cover name real items."""
        if self.is_document_request:
            absent = [name for name in ("title", "document_type", "category") if not getattr(self, name)]
            if absent:
                raise ValueError(f"document requests must set: {', '.join(absent)}")
        requested_ids = {item.id for item in self.requested_items}
        unknown = sorted({m.item_id for m in self.missing_info} - requested_ids)
        if unknown:
            raise ValueError(f"missing_info refers to unknown requested items: {', '.join(unknown)}")
        unknown = sorted(set(self.assumptions_cover) - requested_ids)
        if unknown:
            raise ValueError(f"assumptions_cover refers to unknown requested items: {', '.join(unknown)}")
        if self.assumptions_cover and not self.assumptions:
            raise ValueError("assumptions_cover needs at least one assumption")
        return self

    def covered_by_assumptions(self) -> set[str]:
        """Requested items the automatic Assumptions section fulfils (only when assumptions exist)."""
        return set(self.assumptions_cover) if self.assumptions else set()


# --------------------------------------------------------------------------- document content


class Item(LLMModel):
    """A structured entry (bullet or table row) whose facts can be grounded."""

    type: ItemType
    kind: ItemKind = Field(description="record = happened or was agreed; proposal = agent recommendation")
    text: NonEmptyStr
    owner: str | None = None
    due: str | None = Field(None, description="Deadline, date or time slot")
    value: str | None = Field(None, description="Amount or figure")
    source_quote: str | None = Field(None, description="Verbatim request text supporting a record item")
    label: ProposalLabel | None = Field(None, description="Required on every proposal item")
    assumption_id: AssumptionId | None = Field(
        None, description="Only when that specific assumption supports the item"
    )


class ColumnLabels(LLMModel):
    """Header text for the table columns built from item fields."""

    text: NonEmptyStr
    owner: str | None = None
    due: str | None = None
    value: str | None = None


class SectionContent(LLMModel):
    """Output of the write_section tool. The heading comes from the task title."""

    paragraphs: list[NonEmptyStr] = Field(default_factory=list)
    items: list[Item] = Field(default_factory=list)


class TableContent(LLMModel):
    """Output of the build_table tool. The heading comes from the task title."""

    intro: str | None = None
    columns: ColumnLabels
    rows: list[Item]


class Table(BaseModel):
    """A rendered table: column labels plus grounded rows."""

    columns: ColumnLabels
    rows: list[Item]


class Section(BaseModel):
    """One document section, produced by a task and later grounded, reviewed and rendered."""

    task_id: TaskId
    heading: NonEmptyStr
    paragraphs: list[str] = Field(default_factory=list)
    items: list[Item] = Field(default_factory=list)
    table: Table | None = None
    required: bool = True
    covers: list[RequestedItemId] = Field(default_factory=list)
    missing_source_note: str | None = None


# --------------------------------------------------------------------------- review


class ReviewIssue(LLMModel):
    """A problem the reviewer found in one section."""

    section_id: TaskId
    type: IssueType
    severity: Severity
    problem: NonEmptyStr


class ReviewResult(LLMModel):
    """Output of a review or verify call."""

    issues: list[ReviewIssue] = Field(default_factory=list)


# --------------------------------------------------------------------------- API


class AgentRequest(BaseModel):
    """Body of POST /agent."""

    request: NonEmptyStr = Field(examples=["Create meeting minutes for our Monday product sync..."])

    @field_validator("request")
    @classmethod
    def _within_length_limit(cls, value: str) -> str:
        limit = get_settings().max_request_chars
        if len(value) > limit:
            raise ValueError(f"request must be at most {limit} characters")
        return value


class TaskReport(BaseModel):
    """Outcome of one planned task, as shown to the user."""

    id: str
    title: str
    tool: str
    required: bool
    status: TaskStatus
    detail: str | None = None


class UsageReport(BaseModel):
    """LLM usage for one request."""

    llm_calls: int
    failed_attempts: int
    tokens_used: int
    duration_s: float


class AgentResponse(BaseModel):
    """Body returned by POST /agent."""

    status: RunStatus
    message: str
    title: str | None = None
    document_url: str | None = None
    plan: list[TaskReport] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    usage: UsageReport | None = None
