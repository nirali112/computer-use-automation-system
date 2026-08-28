"""How a recorded step says which control it means.

This is the part of the artifact that decides whether a replay still works
next month, so it gets its own module.

The design follows from what the target surface actually offers. Probing the
mock console's accessibility tree produced three distinct situations:

    search screen        textbox "Member ID"      -- role and name identify it
    member detail        cell "Savings",
                         cell "$4,182.55"         -- meaning comes from the row
    sub-account form     textbox "",  textbox ""  -- two anonymous, identical
                                                     controls

The third case is the ordinary one in legacy enterprise software, and it
rules out any single targeting strategy: those two inputs cannot be told
apart by role and name because neither has a name. What distinguishes them
is the text in the table cell beside them.

So a target is not one locator. It is an ordered list of strategies, each
carrying its own rationale, tried in turn until one resolves. Recording the
rationale matters as much as recording the strategy: a reviewer needs to see
why the recorder believed a strategy would hold, and a replay failure needs
to be able to say which assumption broke.

Strategies are deliberately expressed in terms of role, accessible name and
structural relationships -- never CSS selectors or XPath. That vocabulary is
not a web convenience; it is the vocabulary the platform accessibility APIs
also speak, which is what lets the same artifact shape describe a desktop
application later.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

Confidence = Literal["high", "medium", "low"]


class StrategyBase(BaseModel):
    rationale: str = Field(
        description="Why this identifies the control, and what would break it. "
        "Written at record time; read by a human reviewer and by whoever "
        "debugs a replay failure."
    )
    confidence: Confidence = Field(
        description="How durable the recorder judged this strategy to be. "
        "Replay reports the confidence of whichever strategy resolved, so a "
        "capability quietly surviving on its last resort is visible."
    )


class RoleName(StrategyBase):
    """Match on the control's role and its accessible name.

    The strongest strategy available, because the browser -- not us --
    computed the name, resolving label associations, title attributes and
    inner text along the way. It is also the strategy that ports directly to
    desktop accessibility APIs, where role and name are the primitives.
    """

    kind: Literal["role_name"] = "role_name"
    role: str
    name: str
    match: Literal["exact", "contains"] = "exact"


class CellAdjacent(StrategyBase):
    """Match a control by the text of the table cell next to it.

    For form fields that carry no label of any kind, the visible meaning sits
    in a neighbouring cell. This reads the layout table the way an operator
    does. It survives restyling and renaming of the underlying control, and
    breaks if the form is reorganised -- which in these applications is rare
    and, when it happens, is a real change worth failing on.
    """

    kind: Literal["cell_adjacent"] = "cell_adjacent"
    role: str
    label_text: str
    match: Literal["exact", "contains"] = "exact"


class Ordinal(StrategyBase):
    """Match the Nth control of a role within the frame.

    An explicit last resort. It is positional, so any inserted control ahead
    of it silently changes what it points at. It is included because
    sometimes nothing better exists, and it is better to record that fact
    with low confidence than to pretend a fragile locator is a robust one.
    """

    kind: Literal["ordinal"] = "ordinal"
    role: str
    index: int = Field(ge=0, description="Zero-based, in document order.")


Strategy = Annotated[Union[RoleName, CellAdjacent, Ordinal], Field(discriminator="kind")]


class Target(BaseModel):
    """A control the automation acts on, plus how to find it again."""

    description: str = Field(
        description="What this control is, in the language of the application "
        "-- 'the Initial Deposit field'. Written for a human reviewer."
    )
    frame: str = Field(
        default="mainFrame",
        description="Which frame of the surface the control lives in. These "
        "consoles are framesets, so there is no single document, and "
        "resolution has to be scoped to a frame rather than a page.",
    )
    strategies: list[Strategy] = Field(
        min_length=1,
        description="Ordered. Replay tries each in turn and uses the first "
        "that resolves to exactly one control. A strategy matching several "
        "controls is treated as a failure, not as a reason to guess.",
    )
