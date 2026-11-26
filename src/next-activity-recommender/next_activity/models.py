"""Request and response contracts shared with the HobbyTree backend."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

PositiveId = Annotated[int, Field(gt=0)]
StageNumber = Annotated[int, Field(ge=1, le=6)]
Amount = Annotated[int, Field(ge=0)]
Difficulty = Literal["EASY", "APPROPRIATE", "HARD"]
Signal = Literal[
    "PREFER_SHORTER", "PREFER_LOWER_COST", "PREFER_FEWER_TOOLS", "PREFER_ONE_PAN"
]


class Contract(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class InvalidRequest(Contract):
    detail: str
    types: list[str]


class AuthenticationError(Contract):
    detail: str


class FeedbackRequest(Contract):
    recordId: PositiveId
    status: Literal["COMPLETED", "PARTIAL", "NOT_DONE"]
    satisfactionScore: Annotated[int, Field(ge=1, le=5)] | None = None
    perceivedDifficulty: Difficulty | None = None
    memo: Annotated[str, Field(max_length=1000)] | None = None
    notDoneReason: Annotated[str, Field(min_length=1, max_length=200)] | None = None

    @model_validator(mode="after")
    def check_completion_fields(self) -> Self:
        if any(text and "\x00" in text for text in (self.memo, self.notDoneReason)):
            raise ValueError("Feedback text may not contain NUL")
        includes_feedback = any(
            value is not None
            for value in (self.satisfactionScore, self.perceivedDifficulty, self.memo)
        )
        if self.status != "NOT_DONE" or includes_feedback:
            if self.satisfactionScore is None or self.perceivedDifficulty is None:
                raise ValueError(
                    "Provide the recorded score and perceived difficulty together"
                )
        if self.notDoneReason is not None and not self.notDoneReason.strip():
            raise ValueError("A supplied reason must contain text")
        if self.status == "NOT_DONE" and self.notDoneReason is None:
            raise ValueError("A reason is required for NOT_DONE")
        return self


class FeedbackResult(Contract):
    recordId: PositiveId
    difficulty: Difficulty | None
    sentiment: Literal["POSITIVE", "NEGATIVE", "MIXED", "UNKNOWN"]
    signals: list[Signal]
    evidence: list[str]


class Candidate(Contract):
    activityId: PositiveId
    hobbyId: PositiveId
    stageId: PositiveId
    stageNumber: StageNumber
    title: Annotated[str, Field(min_length=1, pattern=r"\S")]
    published: bool
    startable: bool
    durationMinutes: Amount | None = None
    estimatedCost: Amount | None = None
    requiredToolCount: Amount | None = None
    onePan: bool | None = None


class RankRequest(Contract):
    treeStatus: Literal["ACTIVE", "PAUSED", "ENDED"]
    hobbyId: PositiveId
    currentStageNumber: StageNumber
    currentRecordId: PositiveId | None = None
    availableMinutes: Amount | None = None
    budgetKrw: Amount | None = None
    completedActivityIds: list[PositiveId] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)
    preferredDifficulty: Difficulty | None = Field(
        default=None, description="Recent perceived difficulty reported by the user."
    )
    candidates: list[Candidate]
    limit: Annotated[int, Field(ge=1, le=2)] = 2

    @model_validator(mode="after")
    def check_ids(self) -> Self:
        if len({c.activityId for c in self.candidates}) != len(self.candidates):
            raise ValueError("Candidate activity IDs must be unique")
        if len(self.signals) != len(set(self.signals)):
            raise ValueError("Supply each ordered preference signal once")
        return self


class RecommendedItem(Contract):
    activityId: PositiveId
    stageId: PositiveId
    stageNumber: StageNumber
    title: str
    durationMinutes: Amount | None
    reason: str


class RankResult(Contract):
    status: Literal["RECOMMENDED", "RESUME", "INACTIVE", "NO_MATCH"]
    resumeRecordId: PositiveId | None
    items: list[RecommendedItem]
