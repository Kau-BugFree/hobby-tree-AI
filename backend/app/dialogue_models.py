"""Bounded model contracts and persisted dialogue state."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Observation(Contract):
    id: str
    source_message_id: str | None
    quote: str
    status: Literal['reported', 'unknown', 'superseded'] = 'reported'
    supersedes: list[str] = Field(default_factory=list)
    question_snapshot: dict | None = None
    selected_option_index: int | None = None
    answer_text: str | None = None
    provenance: Literal['user_quote', 'server_option', 'legacy'] = 'user_quote'


class Requirement(Contract):
    id: str
    text: str = Field(max_length=1000)
    kind: Literal['lookup', 'procedure', 'cause', 'remedy', 'assessment'] = 'lookup'
    source_message_id: str | None


class EvidenceQuote(Contract):
    evidence_id: str
    quote: str = Field(min_length=1)


class RequirementCoverage(Contract):
    requirement_id: str
    status: Literal['supported', 'missing_observation', 'missing_knowledge', 'conflicting', 'unassessed'] = 'unassessed'
    evidence_ids: list[str] = Field(default_factory=list, max_length=10)
    quotes: list[EvidenceQuote] = Field(default_factory=list, max_length=10)


class ObservationUpdate(Contract):
    quote: str = Field(min_length=1, max_length=3000)
    supersedes: list[str] = Field(default_factory=list, max_length=64)
    status: Literal['reported', 'unknown'] = 'reported'


class RequirementProposal(Contract):
    text: str = Field(min_length=1, max_length=1000)
    kind: Literal['lookup', 'procedure', 'cause', 'remedy', 'assessment'] = 'lookup'
    source_quote: str = Field(min_length=1, max_length=3000)


class TurnInterpretation(Contract):
    route: Literal['action', 'lookup', 'recommend', 'dialogue', 'meta', 'reflection', 'out_of_scope']
    transition: Literal['continue', 'correct', 'new_issue', 'resolve', 'abandon', 'none'] = 'none'
    transition_quote: str = ''
    observation_updates: list[ObservationUpdate] = Field(default_factory=list, max_length=8)
    requirement_proposals: list[RequirementProposal] = Field(default_factory=list, max_length=8)


class DialogueState(Contract):
    version: Literal[2] = 2
    issue_id: str | None = None
    activity_id: str | None = None
    execution_id: str | None = None
    phase: Literal['idle', 'open', 'awaiting_observation', 'answer_provided', 'resolved', 'abandoned'] = 'idle'
    goal: str = Field(default='', max_length=1000)
    requirements: list[Requirement] = Field(default_factory=list, max_length=8)
    observations: list[Observation] = Field(default_factory=list, max_length=64)
    pending_question: dict | None = None
    evidence_ids: list[str] = Field(default_factory=list, max_length=10)
    coverage: list[RequirementCoverage] = Field(default_factory=list, max_length=8)
    clarification_count: int = Field(default=0, ge=0, le=2)
    reflection_count: int = Field(default=0, ge=0, le=1)
    searched_queries: list[dict] = Field(default_factory=list, max_length=16)
    outcome: dict | None = None
    step_number: int | None = None
    asked_questions: list[str] = Field(default_factory=list)
    hop_trace: list[dict] = Field(default_factory=list)


class Evidence(Contract):
    evidence_id: str
    activity_id: str
    title: str = ''
    review_status: Literal['draft', 'source_only', 'reviewed'] = 'source_only'
    kind: str
    excerpt: str
    source_refs: list = Field(default_factory=list)
    applies_when: str = ''


class AnswerClaim(Contract):
    text: str = Field(min_length=1, max_length=3000)
    evidence_ids: list[str] = Field(min_length=1, max_length=5)
    quotes: list[EvidenceQuote] = Field(min_length=1, max_length=10)


class RecommendationSnapshot(Contract):
    message_id: str
    query: str
    scope: str
    constraints: dict
    candidate_ids: list[str]
    ranking: list[dict]
    search_mode: str


class ClaimAnswer(Contract):
    claims: list[AnswerClaim] = Field(min_length=1, max_length=8)


class ClaimVerdict(Contract):
    claim_index: int = Field(ge=0, le=7)
    verdict: Literal['supported', 'contradicted', 'insufficient']
    evidence_ids: list[str] = Field(default_factory=list, max_length=5)


class ClaimVerification(Contract):
    verdicts: list[ClaimVerdict] = Field(min_length=1, max_length=8)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    next_action: Literal["clarify", "retrieve", "answer", "abstain"]
    query: str = Field(default="", max_length=1000)
    depends_on: list[str] = Field(default_factory=list, max_length=10)
    question: str = Field(default="", max_length=400)
    options: list[str] = Field(default_factory=list, max_length=4)
    evidence_ids: list[str] = Field(default_factory=list, max_length=5)
    reset_issue: bool = False
    target_requirement_ids: list[str] = Field(default_factory=list, max_length=8)
    coverage: list[RequirementCoverage] = Field(default_factory=list, max_length=8)
    question_purpose: Literal['clarification', 'reflection', 'transition'] = 'clarification'


def new_dialogue() -> dict:
    return {**DialogueState().model_dump(), 'status': 'idle'}
