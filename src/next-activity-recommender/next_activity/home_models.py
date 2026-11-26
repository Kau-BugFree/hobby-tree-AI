"""Wire contract for home recommendations with an actionable empty-catalog fallback."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .absa_models import ActivityGroup, GroupPreference
from .models import (
    Amount,
    Candidate,
    Contract,
    PositiveId,
    RankRequest,
    RecommendedItem,
)


class HomeCandidate(Candidate):
    activityGroup: ActivityGroup | None = Field(
        default=None, description="Stable catalog group shared with historical records."
    )


class HomeRankRequest(RankRequest):
    candidates: list[HomeCandidate] = Field(
        description="All scanned candidate pages. limit controls only the number returned."
    )
    groupPreferences: list[GroupPreference] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_group_preferences(self) -> Self:
        keys = {(p.activityGroup, p.category) for p in self.groupPreferences}
        if len(keys) != len(self.groupPreferences):
            raise ValueError("Each group and category pair may appear only once")
        return self


class HomeItem(RecommendedItem):
    estimatedCost: Amount | None
    score: Annotated[
        float, Field(ge=0, le=100, description="Policy points, not a probability.")
    ]
    tier: Literal["STANDARD", "RELAXED", "OPEN"]
    budgetStatus: Literal["UNSET", "WITHIN", "OVER", "UNKNOWN"]
    isRepeat: bool


class SelfGuidedActivity(Contract):
    kind: Literal["SELF_GUIDED"]
    title: str
    instruction: str


class HomeRankResult(Contract):
    status: Literal["RECOMMENDED", "RESUME", "INACTIVE", "SELF_GUIDED"]
    resumeRecordId: PositiveId | None
    items: Annotated[list[HomeItem], Field(max_length=2)]
    alternative: SelfGuidedActivity | None
    evaluatedCount: Amount = Field(
        description="Size of the valid pool actually scored before the display limit."
    )
    priceFallback: bool = Field(
        description="True when prices are ordered within fit bands because no known price fits the stated budget."
    )

    @model_validator(mode="after")
    def check_display(self) -> Self:
        expected = (
            self.status == "RECOMMENDED",
            self.status == "RESUME",
            self.status in {"SELF_GUIDED", "INACTIVE"},
        )
        if (
            bool(self.items),
            self.resumeRecordId is not None,
            self.alternative is not None,
        ) != expected:
            raise ValueError(
                "A home response must provide exactly the display content specified by its status"
            )
        return self
