"""Domain aspect observations and the text-free history contract."""

import re
from datetime import datetime
from typing import Annotated, Literal, Self, get_args

from pydantic import Field, StringConstraints, field_validator, model_validator

from .models import Contract, Difficulty, PositiveId, Signal

Category = Literal[
    "ENJOYMENT", "DIFFICULTY", "DURATION", "COST", "TOOLS", "COOKING_METHOD"
]
Polarity = Literal["POSITIVE", "NEGATIVE", "NEUTRAL", "MIXED"]
CATEGORY_ORDER = get_args(Category)
ActivityGroup = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
    Field(
        description="Stable BE catalog group shared by an activity record and candidate."
    ),
]
GroupCategory = Literal["ENJOYMENT", "DIFFICULTY"]
GroupPolarity = Literal["POSITIVE", "NEGATIVE"]

_CATEGORY_SIGNALS = {
    "DURATION": "PREFER_SHORTER",
    "COST": "PREFER_LOWER_COST",
    "TOOLS": "PREFER_FEWER_TOOLS",
    "COOKING_METHOD": "PREFER_ONE_PAN",
}


class Evidence(Contract):
    source: Literal["memo", "notDoneReason"]
    text: Annotated[str, Field(min_length=1)]
    start: Annotated[
        int, Field(ge=0, description="Inclusive Unicode code point offset in source.")
    ]
    end: Annotated[
        int, Field(gt=0, description="Exclusive Unicode code point offset in source.")
    ]

    @model_validator(mode="after")
    def check_span_length(self) -> Self:
        if self.end - self.start != len(self.text):
            raise ValueError("Offsets must describe the length of the evidence")
        return self


class AspectSummary(Contract):
    category: Category
    polarity: Polarity
    signal: Signal | None = None

    @model_validator(mode="after")
    def check_signal(self) -> Self:
        if self.signal is None:
            return self
        if self.signal != _CATEGORY_SIGNALS.get(self.category):
            raise ValueError("Signal does not belong to this aspect")
        if self.polarity in {"MIXED", "NEUTRAL"}:
            raise ValueError("An unresolved observation cannot supply a preference")
        if self.category == "COOKING_METHOD" and self.polarity != "POSITIVE":
            raise ValueError("A one-pan preference requires positive evidence")
        return self


class Aspect(AspectSummary):
    evidence: Annotated[list[Evidence], Field(min_length=1)]


def _check_categories(aspects: list) -> None:
    if len({aspect.category for aspect in aspects}) != len(aspects):
        raise ValueError("Each record may have at most one observation per category")


class AnalysisResult(Contract):
    recordId: PositiveId
    reportedDifficulty: Difficulty | None
    aspects: list[Aspect]

    @model_validator(mode="after")
    def check_aspects(self) -> Self:
        _check_categories(self.aspects)
        return self


# Preserve the original fractional digits as BE can emit nanosecond timestamps.
_TIMESTAMP = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(\.[0-9]+)?([Zz]|[+-]([01][0-9]|2[0-3]):[0-5][0-9])$"
)


class HistoryRecord(Contract):
    userHobbyId: PositiveId
    recordId: PositiveId
    completedAt: Annotated[
        str, Field(pattern=_TIMESTAMP, json_schema_extra={"format": "date-time"})
    ]
    reportedDifficulty: Difficulty | None = None
    aspects: list[AspectSummary]

    @field_validator("completedAt")
    @classmethod
    def check_timestamp(cls, value: str) -> str:
        datetime.fromisoformat(value.upper())
        return value

    @model_validator(mode="after")
    def check_aspects(self) -> Self:
        _check_categories(self.aspects)
        return self

    def chronology_key(self) -> tuple[datetime, str, int]:
        second = datetime.fromisoformat(self.completedAt.upper()).replace(microsecond=0)
        fraction = re.search(r"\.(\d+)", self.completedAt)
        digits = fraction.group(1).rstrip("0") if fraction else ""
        return second, digits, self.recordId


class HistoryRequest(Contract):
    userHobbyId: PositiveId
    records: list[HistoryRecord] = Field(
        description="Current, nondeleted records of a single user's hobby tree."
    )

    @model_validator(mode="after")
    def check_records(self) -> Self:
        if any(record.userHobbyId != self.userHobbyId for record in self.records):
            raise ValueError("All records must belong to the requested hobby tree")
        if len({record.recordId for record in self.records}) != len(self.records):
            raise ValueError("History record IDs must be unique")
        return self


class ProfileResult(Contract):
    userHobbyId: PositiveId
    recordCount: Annotated[int, Field(ge=0)]
    preferredDifficulty: Difficulty | None
    signals: list[Signal]


class GroupedHistoryRecord(HistoryRecord):
    activityGroup: ActivityGroup | None = None


class GroupedHistoryRequest(HistoryRequest):
    records: list[GroupedHistoryRecord] = Field(
        description="Current, nondeleted records of one tree with optional catalog group keys."
    )


class GroupPreference(Contract):
    activityGroup: ActivityGroup
    category: GroupCategory
    polarity: GroupPolarity


class GroupedProfileResult(ProfileResult):
    groupPreferences: list[GroupPreference]
