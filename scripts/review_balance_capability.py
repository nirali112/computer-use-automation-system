"""The review a person performs on a freshly discovered capability.

A discovery run sees only the path that worked, so what it produces is a
draft: the flow, the targeting, the parameters, the outputs and a checkpoint,
and nothing about what happens when things go otherwise. Those are the
reviewer's contribution, and this is that contribution written down.

It is a script rather than a hand-edited file for two reasons. It is diffable,
so what review added to the recording is visible in one place. And it is
re-runnable, so re-recording the capability after a vendor upgrade does not
mean re-deciding all of this from memory.

What a reviewer adds, and why a machine could not:

  business outcomes  which of the application's non-success answers callers
                     need to hear about, and what to call each one. That is a
                     judgement about the caller's domain, not an observation
                     about the screen
  recovery           which interruptions are safe to resolve unattended, and
                     how many attempts are reasonable
  failure signals    what the application looks like when it has broken, as
                     opposed to when it is refusing
  approval           that a person has read what this does before it runs
                     unattended

    python scripts/review_balance_capability.py
"""

from __future__ import annotations

import sys

from cua.artifact import (
    BusinessOutcome,
    Condition,
    Dismiss,
    FailureSignal,
    Reauthenticate,
    Recovery,
    RoleName,
    Target,
    TextPresent,
    load_latest,
    save,
)

CAPABILITY_ID = "member_savings_balance"


def step_after(capability, phrase: str) -> int:
    """Find the step an outcome can first appear after, by what it does.

    Located by intent rather than by index on purpose: the recording is
    regenerated whenever the capability is re-discovered, and step numbers
    move. What the step is for does not.
    """
    for step in capability.steps:
        if phrase in step.intent.lower():
            return step.index
    raise SystemExit(f"no step in {capability.id} is described as {phrase!r}; review it by hand")


def main() -> int:
    capability = load_latest(CAPABILITY_ID)
    if capability.approval == "approved":
        print(f"{capability.id} v{capability.version} is already approved; nothing to do")
        return 0

    reviewed = capability.model_copy(deep=True)
    reviewed.version = capability.version + 1

    reviewed.business_outcomes = [
        BusinessOutcome(
            code="MEMBER_NOT_FOUND",
            description=(
                "No member exists with the supplied ID. A legitimate answer the caller "
                "needs in order to act, not a failure of the automation."
            ),
            detect=Condition(
                description="the search screen reports no matching record",
                assertions=[TextPresent(text="No record found")],
            ),
            after_step=step_after(reviewed, "search"),
        ),
        BusinessOutcome(
            code="SIGNON_FAILED",
            description="The supplied operator credentials were rejected by the console.",
            detect=Condition(
                description="the sign-on screen reports a failed sign-on",
                assertions=[TextPresent(text="Sign-on failed")],
            ),
            after_step=step_after(reviewed, "sign on"),
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
        Recovery(
            name="reauthenticate_expired_session",
            description=(
                "Sessions expire on inactivity. This capability only reads, so replaying it "
                "from the start changes nothing -- which is what makes re-authentication safe "
                "here and would not make it safe in a flow that writes."
            ),
            detect=Condition(description="the console reports the session has expired",
                             assertions=[TextPresent(text="session has expired")]),
            remedy=Reauthenticate(restart_from=0),
            max_attempts=1,
        ),
    ]

    reviewed.failure_signals = [
        FailureSignal(
            code="CONSOLE_ERROR",
            description=(
                "The console raised an unhandled application error. Nothing the automation "
                "did caused it and nothing it can do will clear it."
            ),
            detect=Condition(description="the console is showing an application error page",
                             assertions=[TextPresent(text="Application Error")]),
        ),
    ]

    reviewed.provenance.notes = (
        (capability.provenance.notes or "")
        + " Reviewed by hand: business outcomes, recovery rules and failure signals added, "
          "and the capability approved for unattended replay."
    )
    reviewed.approval = "approved"

    path = save(reviewed)
    print(f"reviewed {capability.id}: v{capability.version} ({capability.approval}) "
          f"-> v{reviewed.version} ({reviewed.approval})")
    print(f"  added {len(reviewed.business_outcomes)} business outcomes, "
          f"{len(reviewed.recovery)} recovery rules, "
          f"{len(reviewed.failure_signals)} failure signals")
    print(f"  saved {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
