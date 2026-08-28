"""The ordered actions a capability performs.

Two things here are worth more than they might look.

Values are bound, not baked. A step never carries a literal member ID; it
carries a reference to a declared parameter. That is what makes a recording
a reusable capability rather than a transcript of one particular run, and it
is also what keeps sensitive values out of the stored artifact entirely --
a password is a parameter reference, so there is nothing to leak.

Every step declares its risk. The system has to treat a search differently
from an irreversible submission, and that judgement belongs in the artifact
where a human reviewer can see and change it, not buried in the policy
engine's heuristics.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from .conditions import Checkpoint
from .targeting import Target

Risk = Literal["safe", "irreversible"]


class LiteralValue(BaseModel):
    """A constant recorded from the discovery run."""

    kind: Literal["literal"] = "literal"
    value: str


class ParamValue(BaseModel):
    """A value supplied by the caller at invocation time."""

    kind: Literal["param"] = "param"
    param: str


Value = Annotated[Union[LiteralValue, ParamValue], Field(discriminator="kind")]


class Navigate(BaseModel):
    kind: Literal["navigate"] = "navigate"
    url: str


class Click(BaseModel):
    kind: Literal["click"] = "click"
    target: Target


class TypeText(BaseModel):
    kind: Literal["type"] = "type"
    target: Target
    value: Value


class SelectOption(BaseModel):
    kind: Literal["select"] = "select"
    target: Target
    value: Value


class WaitFor(BaseModel):
    """Wait for a condition rather than for a duration.

    Recording sleeps would make replay both slow and flaky. Waiting on an
    observable condition is what makes it deterministic: the step completes
    exactly when the surface is ready, and fails with a specific expectation
    when it never becomes ready.
    """

    kind: Literal["wait_for"] = "wait_for"
    condition: Checkpoint
    timeout_ms: int = Field(default=10_000, ge=100)


Action = Annotated[
    Union[Navigate, Click, TypeText, SelectOption, WaitFor], Field(discriminator="kind")
]


class Step(BaseModel):
    index: int = Field(ge=0)
    intent: str = Field(
        description="What this step is for, in plain language. Replay failures "
        "quote it, so it is the first thing a human reads when something "
        "breaks -- 'enter the member ID into the search form'."
    )
    action: Action
    risk: Risk = Field(
        default="safe",
        description="Whether this step changes state the institution cannot "
        "trivially undo. Policy decides what to do about it; the artifact "
        "records the judgement so a reviewer can see and revise it.",
    )
    expect: Checkpoint | None = Field(
        default=None,
        description="Asserted after the action. Absent for steps whose effect "
        "is verified by the following step's own precondition.",
    )
