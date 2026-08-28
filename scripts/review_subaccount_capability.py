"""The review of the sub-account capability.

Everything the balance review adds, plus the one thing this capability needs
that the other did not: a judgement about which of its steps cannot be undone.

That judgement is the clearest illustration of why review exists. The
discovery run submitted the request and reached the confirmation screen
without anything stopping it, and recorded the step as safe -- because nothing
on the screen distinguishes a button that opens an account from a button that
runs a search. Knowing the difference requires knowing what the application
does, which is a fact about the institution's business rather than about the
user interface. A recording cannot supply it. A person can.

Marking it is what brings the guardrail into play: from here the step is
blocked unless policy permits irreversible actions, the capability is
approved, and the caller authorises it for that specific invocation -- or a
named operator authorises it on the live session.

Re-authentication is deliberately not offered. Restarting this flow after its
submission has run would open a second account, so the only safe response to
an expired session mid-write is to stop and ask a person.

    python scripts/review_subaccount_capability.py
"""

from __future__ import annotations

import sys

from cua.artifact import (
    BusinessOutcome,
    Condition,
    Dismiss,
    FailureSignal,
    Recovery,
    RoleName,
    Target,
    TextPresent,
    load_latest,
    save,
)

CAPABILITY_ID = "open_member_subaccount"


def step_doing(capability, phrase: str):
    for step in capability.steps:
        if phrase in step.intent.lower():
            return step
    raise SystemExit(f"no step in {capability.id} is described as {phrase!r}; review it by hand")


def main() -> int:
    capability = load_latest(CAPABILITY_ID)
    if capability.approval == "approved":
        print(f"{capability.id} v{capability.version} is already approved; nothing to do")
        return 0

    reviewed = capability.model_copy(deep=True)
    reviewed.version = capability.version + 1

    # The judgement a recording cannot make.
    submission = step_doing(reviewed, "submit")
    submission.risk = "irreversible"

    reviewed.business_outcomes = [
        BusinessOutcome(
            code="MEMBER_NOT_FOUND",
            description="No member exists with the supplied ID.",
            detect=Condition(description="the search screen reports no matching record",
                             assertions=[TextPresent(text="No record found")]),
            after_step=step_doing(reviewed, "search").index,
        ),
        BusinessOutcome(
            code="SIGNON_FAILED",
            description="The supplied operator credentials were rejected by the console.",
            detect=Condition(description="the sign-on screen reports a failed sign-on",
                             assertions=[TextPresent(text="Sign-on failed")]),
            after_step=step_doing(reviewed, "sign on").index,
        ),
        BusinessOutcome(
            code="SERVICING_NOT_PERMITTED",
            description=(
                "This member is flagged for restricted servicing and this operator may not "
                "open accounts for them. A decision the institution has already made, which "
                "the caller needs to hear rather than have retried."
            ),
            detect=Condition(description="the console refuses the request",
                             assertions=[TextPresent(text="not authorised")]),
            after_step=step_doing(reviewed, "sub-account request form").index,
        ),
        BusinessOutcome(
            code="DEPOSIT_REJECTED",
            description=(
                "The console rejected the opening deposit or the details supplied. The caller "
                "can correct the request and try again; nothing has been opened."
            ),
            detect=Condition(description="the form reports a validation error",
                             assertions=[TextPresent(text="Initial Deposit must")]),
            after_step=submission.index,
        ),
    ]

    reviewed.recovery = [
        Recovery(
            name="dismiss_maintenance_notice",
            description=(
                "The console interposes a maintenance notice at unpredictable points. It "
                "carries nothing the caller needs and is cleared by its own Continue button."
            ),
            detect=Condition(description="a system notice is interposed",
                             assertions=[TextPresent(text="System Notice")]),
            remedy=Dismiss(target=Target(
                description="the Continue button on the notice",
                strategies=[RoleName(role="button", name="Continue", confidence="high",
                                     rationale="The notice renders a single submit control "
                                               "labelled Continue.")])),
            max_attempts=2,
        ),
    ]

    reviewed.failure_signals = [
        FailureSignal(
            code="CONSOLE_ERROR",
            description="The console raised an unhandled application error.",
            detect=Condition(description="the console is showing an application error page",
                             assertions=[TextPresent(text="Application Error")]),
        ),
    ]

    reviewed.provenance.notes = (
        (capability.provenance.notes or "")
        + f" Reviewed by hand: step {submission.index} ({submission.intent}) marked "
          "irreversible, business outcomes and a recovery rule added, and the capability "
          "approved. Re-authentication is deliberately not offered: restarting this flow "
          "after its submission has run would open a second account."
    )
    reviewed.approval = "approved"

    path = save(reviewed)
    print(f"reviewed {capability.id}: v{capability.version} ({capability.approval}) "
          f"-> v{reviewed.version} ({reviewed.approval})")
    print(f"  step {submission.index} ({submission.intent}) marked irreversible")
    print(f"  added {len(reviewed.business_outcomes)} business outcomes, "
          f"{len(reviewed.recovery)} recovery rules, "
          f"{len(reviewed.failure_signals)} failure signals")
    print(f"  saved {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
