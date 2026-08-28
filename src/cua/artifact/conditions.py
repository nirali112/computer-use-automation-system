"""Assertions about the state of the surface.

One vocabulary serves three different jobs in the artifact, and using the
same vocabulary for all three is deliberate:

    checkpoints          confirm the flow actually reached the state it
                         expected, instead of assuming a click worked
    business outcomes    recognise a legitimate answer -- "no record found"
                         -- so it can be reported as a result
    recovery rules       recognise a known, dismissible condition so replay
                         can deal with it and carry on

The same detection machinery is therefore exercised on every run, and the
difference between "success", "a business outcome" and "a fault" becomes a
question of which list an assertion appears in -- not a question of which
ad-hoc piece of code happened to catch it.

Assertions read only what an operator could see. There is no assertion that
inspects markup structure, because the surfaces this has to generalise to do
not have markup.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from .targeting import Target


class TextPresent(BaseModel):
    """Some text is visible in a frame."""

    kind: Literal["text_present"] = "text_present"
    text: str
    frame: str = "mainFrame"
    match: Literal["exact", "contains"] = "contains"


class TextAbsent(BaseModel):
    """Some text is not visible in a frame.

    Needed for checkpoints that have to rule something out: reaching the
    confirmation screen means the confirmation heading is present *and* no
    validation error is.
    """

    kind: Literal["text_absent"] = "text_absent"
    text: str
    frame: str = "mainFrame"
    match: Literal["exact", "contains"] = "contains"


class ControlPresent(BaseModel):
    """A control resolvable by the usual targeting rules exists."""

    kind: Literal["control_present"] = "control_present"
    target: Target


Assertion = Annotated[
    Union[TextPresent, TextAbsent, ControlPresent], Field(discriminator="kind")
]


class Condition(BaseModel):
    """A named group of assertions that must all hold.

    Grouping them under a description keeps failures legible: a replay can
    report "expected: the confirmation screen for a new sub-account" rather
    than dumping three unrelated predicates at whoever is debugging it.
    """

    description: str
    assertions: list[Assertion] = Field(min_length=1)


class Checkpoint(Condition):
    """A condition asserted to prove a state was genuinely reached.

    Every capability has one for overall success, and any individual step may
    carry one. Without these, replay degenerates into firing actions at a
    surface and hoping -- which is the failure mode that makes recorded
    automation untrustworthy in the first place.
    """
