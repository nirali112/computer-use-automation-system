"""What a replay returns.

The distinction this module exists to make is the one the brief calls the
most common design mistake in this kind of system: "no such member" is a
legitimate answer the caller needs, not a crash. Conflating the two produces
automation that raises alerts for ordinary business events and, worse,
teaches its operators to ignore alerts.

So a replay reports one of four things, and the boundaries between them are
the contract:

    SUCCESS           the flow reached its goal; declared outputs returned
    BUSINESS_OUTCOME  the application answered, and the answer was not the
                      goal -- unknown member, rejected deposit, permission
                      denied. The automation worked. The caller branches on
                      a stable code
    FAILED            the automation could not do its job -- a control it
                      could not find, a checkpoint that never held, a
                      recovery that ran out of attempts, an application
                      error. Something is wrong and someone must look
    ESCALATED         it stopped and asked for a human, deliberately

The first two are both successful replays. Only FAILED means something
broke. That is the whole point.

Recoveries are not a fifth category. Dismissing a known interstitial or
waiting out a stall is something replay does *while* succeeding, so it is
recorded on the result rather than becoming the result. A caller that wants
to know how much nursing a capability needed can read `recoveries`; a caller
that just wants the balance can ignore it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Status(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    FAILED = "failed"
    ESCALATED = "escalated"


class FailureKind(str, Enum):
    """Why the automation could not do its job.

    Deliberately specific. "Replay failed" tells whoever is on call nothing;
    "the Initial Deposit field could not be resolved at step 9" tells them
    where to look and what changed.
    """

    INVALID_INPUT = "invalid_input"
    """The caller's arguments did not satisfy the capability's contract.
    Detected before the surface is touched, so a bad call costs nothing."""

    POLICY_BLOCKED = "policy_blocked"
    """A guardrail refused the step. Not a malfunction -- the system working."""

    TARGET_UNRESOLVED = "target_unresolved"
    """No targeting strategy resolved to exactly one control. The report
    names every strategy tried and what each matched."""

    CHECKPOINT_FAILED = "checkpoint_failed"
    """The expected state never arrived within the wait. Names the
    assertions that did not hold, so expected and observed sit together."""

    EXTRACTION_FAILED = "extraction_failed"
    """The flow reached its goal but a declared output could not be read.
    Distinct from a checkpoint failure: the automation drove correctly and
    the contract is still unmet."""

    APPLICATION_ERROR = "application_error"
    """The application itself failed. Not the automation's fault, and worth
    saying so plainly rather than reporting a mysterious timeout."""

    RECOVERY_EXHAUSTED = "recovery_exhausted"
    """A known condition kept recurring past its attempt limit. Bounded on
    purpose: open-ended self-healing is how automation ends up doing things
    nobody sanctioned."""

    UNSAFE_TO_RECOVER = "unsafe_to_recover"
    """A recovery existed but could not be applied without risking harm --
    re-running a flow whose irreversible step has already executed would
    submit it twice."""


@dataclass
class Recovered:
    """A condition replay handled by itself, on the way to its result."""

    rule: str
    at_step: int
    attempt: int
    detail: str


@dataclass
class Failure:
    kind: FailureKind
    step_index: int | None
    intent: str
    expected: str
    observed: str
    detail: str = ""

    def summary(self) -> str:
        where = f"step {self.step_index} ({self.intent})" if self.step_index is not None else "before any step"
        return f"{self.kind.value} at {where}: expected {self.expected}; observed {self.observed}"


@dataclass
class ReplayResult:
    """The whole contract, in one value."""

    status: Status
    capability_id: str
    capability_version: int
    run_id: str
    steps_completed: int = 0
    duration_ms: int = 0

    outputs: dict[str, Any] | None = None
    """Present only on SUCCESS. Typed as the capability declared."""

    outcome_code: str | None = None
    outcome_description: str | None = None
    """Present only on BUSINESS_OUTCOME. The code is the caller's branch key."""

    failure: Failure | None = None
    recoveries: list[Recovered] = field(default_factory=list)
    evidence_dir: str | None = None

    @property
    def replay_worked(self) -> bool:
        """Whether the automation did its job, regardless of what it found.

        The predicate a caller should actually branch on, and the reason it
        exists: `status == SUCCESS` is the wrong test for "did this work",
        and writing it out here stops every caller reinventing it wrongly.
        """
        return self.status in (Status.SUCCESS, Status.BUSINESS_OUTCOME)

    def describe(self) -> str:
        """A one-line summary safe to write anywhere.

        Deliberately names the outputs without quoting them. This string ends
        up in the evidence log, and a summary that helpfully inlined every
        extracted value would undo the care taken everywhere else -- which is
        exactly what a test caught it doing.
        """
        if self.status is Status.SUCCESS:
            names = ", ".join(sorted(self.outputs or {}))
            return f"success: returned {len(self.outputs or {})} output(s): {names}"
        if self.status is Status.BUSINESS_OUTCOME:
            return f"business outcome {self.outcome_code}: {self.outcome_description}"
        if self.status is Status.ESCALATED:
            return "escalated to a human operator"
        return f"failed: {self.failure.summary() if self.failure else 'no detail recorded'}"
