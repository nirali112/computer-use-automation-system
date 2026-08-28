"""A hand-written capability, used to prove the schema and to test replay.

Written by hand rather than recorded, on purpose. Building the replay engine
against an artifact a human authored means replay is proven correct before an
LLM is ever involved -- so when the discovery run does produce an artifact,
any failure is a problem with the recording, not with the executor.

It expresses the first goal from the brief: look up a member and read their
current savings balance.
"""

from cua.artifact import (
    AdjacentCell,
    BusinessOutcome,
    Capability,
    Checkpoint,
    CellAdjacent,
    Click,
    Condition,
    Dismiss,
    LiteralValue,
    Navigate,
    Output,
    Parameter,
    ParamValue,
    Provenance,
    Reauthenticate,
    Recovery,
    RoleName,
    Step,
    Surface,
    TableCell,
    Target,
    TextAbsent,
    TextPresent,
    TypeText,
    WaitFor,
)

BASE_URL = "http://127.0.0.1:8099"


def _labelled(role: str, label: str, description: str) -> Target:
    """A control identified by its label, however the application supplies it.

    Tries the accessible name first, because when the application does
    associate a label the browser has already done the work of resolving it.
    Falls back to the adjacent table cell, which is what the sub-account form
    forces: its inputs surface with an empty accessible name, so the only
    thing distinguishing them is the text in the cell beside them.
    """
    return Target(
        description=description,
        strategies=[
            RoleName(
                role=role,
                name=label,
                confidence="high",
                rationale=(
                    f"The browser computes the accessible name {label!r} when the "
                    "application associates a label with the control. Resolving on "
                    "role and name is the most durable option available and is the "
                    "same primitive desktop accessibility APIs expose."
                ),
            ),
            CellAdjacent(
                role=role,
                label_text=label,
                confidence="medium",
                rationale=(
                    "Falls back to the layout table when the control has no "
                    "accessible name at all, which is the norm in this application. "
                    "Reads the form the way an operator does. Breaks only if the "
                    "form is restructured, which is a real change worth failing on."
                ),
            ),
        ],
    )


def member_balance_capability() -> Capability:
    signon_screen = Checkpoint(
        description="the sign-on screen is displayed",
        assertions=[TextPresent(text="Operator ID")],
    )
    search_screen = Checkpoint(
        description="the member search screen is displayed",
        assertions=[TextPresent(text="Member Search")],
    )

    return Capability(
        id="member_savings_balance",
        version=1,
        name="Read a member's current savings balance",
        description=(
            "Signs on to the Meridian Core servicing console, looks up a member by "
            "their member ID, and returns the member's name and the current balance "
            "of their savings account."
        ),
        surface=Surface(
            kind="web",
            application="Meridian Core",
            application_version="8.2",
            entry_point=f"{BASE_URL}/",
        ),
        parameters=[
            Parameter(
                name="member_id",
                type="string",
                description="The member's identifier, as printed on their statement.",
                pattern=r"^\d{6}$",
                example="100234",
            ),
            Parameter(
                name="operator_id",
                type="string",
                description="Servicing operator to act as.",
                sensitive=True,
            ),
            Parameter(
                name="operator_password",
                type="string",
                description="That operator's password.",
                sensitive=True,
            ),
        ],
        outputs=[
            Output(
                name="member_name",
                type="string",
                description="The member's full name as held on file.",
                extract=AdjacentCell(label_text="Name"),
            ),
            Output(
                name="savings_balance",
                type="number",
                description="Current balance of the member's savings account, in dollars.",
                extract=TableCell(row_contains="Savings", column_header="Current Balance"),
                pattern=r"\$([\d,]+\.\d{2})",
            ),
        ],
        steps=[
            Step(
                index=0,
                intent="open the servicing console",
                action=Navigate(url=f"{BASE_URL}/"),
                expect=signon_screen,
            ),
            Step(
                index=1,
                intent="enter the operator ID",
                action=TypeText(
                    target=_labelled("textbox", "Operator ID", "the Operator ID field"),
                    value=ParamValue(param="operator_id"),
                ),
            ),
            Step(
                index=2,
                intent="enter the operator password",
                action=TypeText(
                    target=_labelled("textbox", "Password", "the Password field"),
                    value=ParamValue(param="operator_password"),
                ),
            ),
            Step(
                index=3,
                intent="sign on",
                action=Click(
                    target=Target(
                        description="the Sign On button",
                        strategies=[
                            RoleName(
                                role="button",
                                name="Sign On",
                                confidence="high",
                                rationale="A submit input's value becomes its accessible name.",
                            )
                        ],
                    )
                ),
                expect=search_screen,
            ),
            Step(
                index=4,
                intent="enter the member ID to search for",
                action=TypeText(
                    target=_labelled("textbox", "Member ID", "the Member ID search field"),
                    value=ParamValue(param="member_id"),
                ),
            ),
            Step(
                index=5,
                intent="run the search",
                action=Click(
                    target=Target(
                        description="the Search button",
                        strategies=[
                            RoleName(
                                role="button",
                                name="Search",
                                confidence="high",
                                rationale="A submit input's value becomes its accessible name.",
                            )
                        ],
                    )
                ),
            ),
            Step(
                index=6,
                intent="open the member record from the results",
                action=Click(
                    target=Target(
                        description="the Open link on the result row",
                        strategies=[
                            RoleName(
                                role="link",
                                name="Open",
                                confidence="medium",
                                rationale=(
                                    "The results grid renders one row per match and a "
                                    "member ID is unique, so a single Open link is "
                                    "expected. Resolution requires exactly one match, so "
                                    "a multi-row result fails loudly rather than opening "
                                    "an arbitrary record."
                                ),
                            )
                        ],
                    )
                ),
            ),
            Step(
                index=7,
                intent="wait for the member detail screen to render",
                action=WaitFor(
                    condition=Checkpoint(
                        description="the member detail screen is displayed",
                        assertions=[TextPresent(text="Member Detail"), TextPresent(text="Accounts")],
                    )
                ),
            ),
        ],
        checkpoint=Checkpoint(
            description="the member's detail screen, showing their accounts, is displayed",
            assertions=[
                TextPresent(text="Member Detail"),
                TextPresent(text="Current Balance"),
                TextAbsent(text="No record found"),
            ],
        ),
        business_outcomes=[
            BusinessOutcome(
                code="MEMBER_NOT_FOUND",
                description=(
                    "No member exists with the supplied ID. A legitimate answer the "
                    "caller needs, not a failure of the automation."
                ),
                detect=Condition(
                    description="the search screen reports no matching record",
                    assertions=[TextPresent(text="No record found")],
                ),
                after_step=5,
            ),
            BusinessOutcome(
                code="SIGNON_FAILED",
                description="The supplied operator credentials were rejected by the console.",
                detect=Condition(
                    description="the sign-on screen reports a failed sign-on",
                    assertions=[TextPresent(text="Sign-on failed")],
                ),
                after_step=3,
            ),
        ],
        recovery=[
            Recovery(
                name="dismiss_maintenance_notice",
                description=(
                    "The console interposes a maintenance notice at unpredictable "
                    "points. It carries no information the caller needs and is cleared "
                    "by its own Continue button."
                ),
                detect=Condition(
                    description="a system notice is interposed",
                    assertions=[TextPresent(text="System Notice")],
                ),
                remedy=Dismiss(
                    target=Target(
                        description="the Continue button on the notice",
                        strategies=[
                            RoleName(
                                role="button",
                                name="Continue",
                                confidence="high",
                                rationale="The notice renders a single submit control labelled Continue.",
                            )
                        ],
                    )
                ),
                max_attempts=2,
            ),
            Recovery(
                name="reauthenticate_expired_session",
                description=(
                    "Sessions expire on inactivity. Signing back on restores the state "
                    "the flow assumed without changing anything, so it is safe to do "
                    "unattended -- unlike most recoveries."
                ),
                detect=Condition(
                    description="the console reports the session has expired",
                    assertions=[TextPresent(text="session has expired")],
                ),
                remedy=Reauthenticate(),
                max_attempts=1,
            ),
        ],
        provenance=Provenance(
            recorded_by="hand-written reference",
            run_id="reference",
            notes=(
                "Authored by hand to exercise the schema and to give the replay "
                "engine something correct to be tested against before any discovery "
                "run exists."
            ),
        ),
        approval="approved",
    )
