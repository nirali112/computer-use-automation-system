"""Deterministic replay: executing a capability without a model in the loop.

This is the production path. An agent invokes a saved capability with typed
arguments; this drives the surface through the recorded steps and returns the
declared outputs, a business outcome, or a failure it can explain. No model is
consulted, so the same inputs produce the same actions every time.

Determinism here is not achieved by recording timings and reproducing them --
that is exactly what makes recorded automation flaky. It comes from three
choices:

  * Nothing waits for a duration. Every wait is for an observable condition,
    so a step completes when the surface is ready and fails with a specific
    unmet expectation when it never becomes ready.

  * Nothing is resolved ambiguously. A targeting strategy that matches
    several controls is refused rather than resolved to the first.

  * Nothing is assumed to have worked. Checkpoints are asserted, so a click
    that silently did nothing fails at the step that did nothing rather than
    three steps later somewhere confusing.

The loop watches for four declared things at once -- the expected state, a
business outcome, a recovery trigger, an application failure signal -- and the
first to hold decides what happens. Detection is uniform; only the response
differs. That is what keeps "no such member is an answer" from being special-
cased somewhere in an exception handler.

Nothing in this module imports a browser. It is given a `Surface` and never
asks what kind it is.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from ..artifact.capability import (
    BusinessOutcome,
    Capability,
    Dismiss,
    FailureSignal,
    Reauthenticate,
    Recovery,
    Retry,
)
from ..artifact.conditions import Condition
from ..artifact.steps import Click, Navigate, ParamValue, SelectOption, Step, TypeText, WaitFor
from ..evidence.recorder import Recorder
from ..resolve import ExtractionError, cast_value, evaluate_condition, extract_value, resolve_target
from ..escalation.broker import Escalator, Intervention
from ..escalation.control import ControlledSurface, SessionControl, describe_change
from ..safety.policy import Policy
from ..surfaces.base import Observation, Surface
from .outcomes import Failure, FailureKind, Recovered, ReplayResult, Status

DEFAULT_STEP_TIMEOUT_MS = 10_000
DEFAULT_POLL_MS = 200

# Failures a person can actually do something about while standing at the
# session. Deliberately not exhaustive: a malformed argument is the caller's
# mistake and no amount of operator attention fixes it, and an unreadable
# output means the flow already finished. Escalating those would train
# operators to dismiss requests, which is how a working escalation path
# quietly stops working.
# How many times one step may call for a person before the run stops asking.
# Recovery is bounded and escalation has to be too: an operator who authorises
# a step that is then blocked by a different gate would otherwise be asked
# again, and again, with the run making no progress between requests.
MAX_ESCALATIONS_PER_STEP = 1

ESCALATABLE = frozenset({
    FailureKind.TARGET_UNRESOLVED,
    FailureKind.CHECKPOINT_FAILED,
    FailureKind.RECOVERY_EXHAUSTED,
    FailureKind.UNSAFE_TO_RECOVER,
    FailureKind.APPLICATION_ERROR,
    FailureKind.POLICY_BLOCKED,
})


class _Blocked(Exception):
    """Internal: a step could not proceed. Carries the failure to report."""

    def __init__(self, failure: Failure) -> None:
        self.failure = failure


@dataclass
class _Settled:
    """How a wait ended.

    `reason` and the payload fields are correlated: a settle with reason
    "outcome" always carries an outcome. The `require_*` accessors state that
    invariant in one place rather than leaving every call site to assume it,
    and turn a violation into a clear assertion instead of an AttributeError
    several frames away.
    """

    reason: str  # "expected" | "outcome" | "recovery" | "app_failure" | "timeout"
    observation: Observation
    outcome: BusinessOutcome | None = None
    recovery: Recovery | None = None
    signal: FailureSignal | None = None

    def require_outcome(self) -> BusinessOutcome:
        assert self.outcome is not None, "settled on an outcome without one"
        return self.outcome

    def require_recovery(self) -> Recovery:
        assert self.recovery is not None, "settled on a recovery without one"
        return self.recovery

    def require_signal(self) -> FailureSignal:
        assert self.signal is not None, "settled on an application failure without a signal"
        return self.signal


class ReplayEngine:
    """Executes capabilities against a surface."""

    def __init__(
        self,
        surface: Surface,
        recorder: Recorder,
        policy: Policy,
        *,
        escalator: Escalator | None = None,
        control: SessionControl | None = None,
        step_timeout_ms: int = DEFAULT_STEP_TIMEOUT_MS,
        poll_ms: int = DEFAULT_POLL_MS,
        step_pause_ms: int = 0,
    ) -> None:
        self.control = control or SessionControl()
        # Wrapped rather than trusted. While a human holds the session the
        # engine is structurally incapable of acting on it, so no retry path
        # can interfere with an operator mid-form by forgetting to check.
        self.surface = ControlledSurface(surface, self.control)
        self.escalator = escalator
        self.recorder = recorder
        # Required rather than optional. An engine constructed without a policy
        # would be an engine with no guardrail, and the one thing that must
        # never be reachable by forgetting an argument is that.
        self.policy = policy
        self.step_timeout_ms = step_timeout_ms
        self.poll_ms = poll_ms
        # Purely for watching. Replay is fast because nothing waits for a
        # duration -- every wait is for a condition -- so a run that takes a
        # second is working as designed and is also impossible to follow. This
        # inserts a deliberate pause between steps when a person is watching,
        # and is zero everywhere else.
        self.step_pause_ms = step_pause_ms

    # -- inputs ------------------------------------------------------------

    def _validate(self, capability: Capability, inputs: dict[str, Any]) -> dict[str, str]:
        """Check the call against the contract before touching the surface.

        A malformed call should cost nothing and should not leave a browser
        halfway through a banking flow. Validating first also means the
        failure names the offending argument rather than surfacing later as a
        field that would not accept its value.
        """
        problems: list[str] = []
        values: dict[str, str] = {}

        for parameter in capability.parameters:
            if parameter.name not in inputs or inputs[parameter.name] is None:
                if parameter.required:
                    problems.append(f"missing required parameter {parameter.name!r}")
                continue
            text = str(inputs[parameter.name])
            if parameter.pattern and not re.fullmatch(parameter.pattern, text):
                # The value itself is never quoted back: a rejected password is
                # still a password, and this message reaches the evidence log.
                problems.append(
                    f"parameter {parameter.name!r} does not match its declared pattern "
                    f"{parameter.pattern!r}"
                )
            values[parameter.name] = text

        unexpected = set(inputs) - {p.name for p in capability.parameters}
        if unexpected:
            problems.append(f"unexpected parameters: {sorted(unexpected)}")

        if problems:
            raise _Blocked(Failure(
                kind=FailureKind.INVALID_INPUT,
                step_index=None,
                intent="validate the caller's arguments",
                expected=f"arguments satisfying the contract of {capability.id}",
                observed="; ".join(problems),
            ))
        return values

    # -- watching the surface ---------------------------------------------

    def _applicable_outcomes(self, capability: Capability, step_index: int) -> list[BusinessOutcome]:
        return [
            o for o in capability.business_outcomes
            if o.after_step is None or step_index >= o.after_step
        ]

    def _look(self, capability: Capability, step_index: int, expect: Condition | None) -> _Settled:
        """One inspection of the surface, against everything declared."""
        observation = self.surface.observe()

        if observation.dialog:
            # The surface answered it conservatively already -- a modal blocks
            # the page, so there is no option to leave it open and decide
            # later. Recording it makes an unexpected dialog visible in the
            # evidence rather than an invisible influence on what follows.
            self.recorder.event("dialog_dismissed", step=step_index, message=observation.dialog)

        for signal in capability.failure_signals:
            if evaluate_condition(observation, signal.detect).holds:
                return _Settled("app_failure", observation, signal=signal)

        for outcome in self._applicable_outcomes(capability, step_index):
            if evaluate_condition(observation, outcome.detect).holds:
                return _Settled("outcome", observation, outcome=outcome)

        for rule in capability.recovery:
            if evaluate_condition(observation, rule.detect).holds:
                return _Settled("recovery", observation, recovery=rule)

        if expect is None or evaluate_condition(observation, expect).holds:
            return _Settled("expected", observation)

        return _Settled("timeout", observation)

    def _wait(self, capability: Capability, step_index: int, expect: Condition | None,
              timeout_ms: int) -> _Settled:
        """Poll until something declared holds, or the wait runs out.

        Waiting on conditions rather than durations is what makes replay both
        fast and deterministic: it proceeds the instant the surface is ready,
        and when it is not, it can say which assertion never held.
        """
        deadline = time.monotonic() + timeout_ms / 1000
        settled = self._look(capability, step_index, expect)
        while settled.reason == "timeout" and time.monotonic() < deadline:
            time.sleep(self.poll_ms / 1000)
            settled = self._look(capability, step_index, expect)
        return settled

    # -- performing one step ----------------------------------------------

    def _value_of(self, value, inputs: dict[str, str]) -> str:
        return inputs[value.param] if isinstance(value, ParamValue) else value.value

    def _resolve(self, step: Step, target) -> Any:
        resolution = resolve_target(self.surface.observe(), target)
        self.recorder.event(
            "target_resolved" if resolution.ok else "target_unresolved",
            step=step.index,
            target=target.description,
            frame=target.frame,
            strategy=resolution.strategy,
            confidence=resolution.confidence,
            attempts=[{"strategy": a.kind, "confidence": a.confidence,
                       "matched": a.matched, "note": a.note} for a in resolution.attempts],
        )
        if not resolution.ok:
            raise _Blocked(Failure(
                kind=FailureKind.TARGET_UNRESOLVED,
                step_index=step.index,
                intent=step.intent,
                expected=f"exactly one control matching {target.description!r}",
                observed=resolution.describe_failure(target),
            ))
        return resolution.control

    def _perform(self, capability: Capability, step: Step, inputs: dict[str, str]) -> None:
        action = step.action

        if isinstance(action, Navigate):
            self.surface.navigate(action.url)
        elif isinstance(action, Click):
            self.surface.invoke(self._resolve(step, action.target))
        elif isinstance(action, TypeText):
            self.surface.enter_text(self._resolve(step, action.target),
                                    self._value_of(action.value, inputs))
        elif isinstance(action, SelectOption):
            self.surface.choose_option(self._resolve(step, action.target),
                                       self._value_of(action.value, inputs))
        elif isinstance(action, WaitFor):
            settled = self._wait(capability, step.index, action.condition, action.timeout_ms)
            if settled.reason == "timeout":
                raise _Blocked(self._checkpoint_failure(step, action.condition, settled.observation))
        else:
            raise TypeError(f"unhandled action: {type(action).__name__}")

        self.recorder.event("action_performed", step=step.index, action=action.kind, intent=step.intent)

    def _checkpoint_failure(self, step: Step, condition: Condition, observation: Observation) -> Failure:
        unmet = evaluate_condition(observation, condition).failed
        return Failure(
            kind=FailureKind.CHECKPOINT_FAILED,
            step_index=step.index,
            intent=step.intent,
            expected=condition.description,
            observed="; ".join(unmet) or "the expected state never held",
        )

    # -- recovery ----------------------------------------------------------

    def _recover(self, capability: Capability, rule: Recovery, step: Step,
                 attempts: dict[str, int], executed_irreversible: bool) -> int | None:
        """Apply a recovery. Returns a step index to jump to, or None to retry here.

        Bounded twice over: by the rule's own attempt limit, and by a refusal
        to restart a flow that has already done something irreversible.
        """
        used = attempts.get(rule.name, 0) + 1
        attempts[rule.name] = used
        if used > rule.max_attempts:
            raise _Blocked(Failure(
                kind=FailureKind.RECOVERY_EXHAUSTED,
                step_index=step.index,
                intent=step.intent,
                expected=f"{rule.name} to clear the condition within {rule.max_attempts} attempts",
                observed=f"the condition recurred on attempt {used}",
                detail=rule.description,
            ))

        self.recorder.event("recovery_triggered", step=step.index, rule=rule.name,
                            attempt=used, remedy=rule.remedy.kind)
        remedy = rule.remedy

        if isinstance(remedy, Dismiss):
            self.surface.invoke(self._resolve(step, remedy.target))
            jump = None
        elif isinstance(remedy, Retry):
            time.sleep(remedy.delay_ms / 1000)
            jump = None
        elif isinstance(remedy, Reauthenticate):
            if executed_irreversible:
                raise _Blocked(Failure(
                    kind=FailureKind.UNSAFE_TO_RECOVER,
                    step_index=step.index,
                    intent=step.intent,
                    expected="to re-establish the session and resume",
                    observed=(
                        "an irreversible step has already run, so replaying the flow "
                        "could submit it a second time"
                    ),
                    detail=rule.description,
                ))
            jump = remedy.restart_from
        else:
            raise TypeError(f"unhandled remedy: {type(remedy).__name__}")

        self._recoveries.append(Recovered(rule=rule.name, at_step=step.index,
                                          attempt=used, detail=rule.description))
        return jump

    # -- outputs -----------------------------------------------------------

    def _extract(self, capability: Capability, observation: Observation) -> dict[str, Any]:
        outputs: dict[str, Any] = {}
        for declared in capability.outputs:
            try:
                raw = extract_value(observation, declared.extract, declared.pattern)
                outputs[declared.name] = cast_value(raw, declared.type)
            except (ExtractionError, ValueError) as error:
                raise _Blocked(Failure(
                    kind=FailureKind.EXTRACTION_FAILED,
                    step_index=None,
                    intent=f"read the declared output {declared.name!r}",
                    expected=declared.description,
                    observed=str(error),
                )) from error
            if declared.sensitive:
                # Registered as a secret the moment it is read, so that
                # anything written later -- the closing summary, the result
                # file, a snapshot -- is scrubbed of it as well. Withholding
                # it from one event is not enough on its own; a test proved
                # that by finding it in the run summary.
                self.recorder.add_secret(str(outputs[declared.name]))
            # Only the shape is recorded. The value goes to the caller, who
            # asked for it; the evidence log has a longer life and a wider
            # audience, and a member's name does not belong in it.
            self.recorder.event(
                "output_extracted",
                name=declared.name,
                type=declared.type,
                value=None if declared.sensitive else outputs[declared.name],
                withheld=declared.sensitive,
            )
        return outputs

    # -- the run -----------------------------------------------------------

    def run(
        self,
        capability: Capability,
        inputs: dict[str, Any],
        *,
        authorise_irreversible: bool = False,
    ) -> ReplayResult:
        """Execute a capability.

        `authorise_irreversible` is per invocation rather than a stored
        setting, so the decision to permit something that cannot be undone is
        made by whoever is asking, at the moment of asking.
        """
        started = time.monotonic()
        self._recoveries: list[Recovered] = []
        self._interventions: list[str] = []
        self._escalations_by_step: dict[int, int] = {}
        completed = 0

        def finish(**kw) -> ReplayResult:
            result = ReplayResult(
                capability_id=capability.id,
                capability_version=capability.version,
                run_id=self.recorder.run_id,
                steps_completed=completed,
                duration_ms=int((time.monotonic() - started) * 1000),
                recoveries=list(self._recoveries),
                interventions=list(self._interventions),
                evidence_dir=str(self.recorder.directory),
                **kw,
            )
            self.recorder.event("run_finished", status=result.status.value,
                                summary=result.describe(), steps_completed=completed,
                                duration_ms=result.duration_ms)
            self.recorder.write_result(result)
            return result

        try:
            values = self._validate(capability, inputs)
        except _Blocked as blocked:
            self.recorder.event("run_rejected", reason=blocked.failure.observed)
            return finish(status=Status.FAILED, failure=blocked.failure)

        self.recorder.event(
            "run_started",
            capability=capability.id,
            version=capability.version,
            approval=capability.approval,
            # Sensitive arguments are named but never shown. The recorder also
            # scrubs their values from everything else it writes, so a leak
            # would need two independent mistakes.
            inputs={name: ("<withheld>" if name in capability.sensitive_parameters() else value)
                    for name, value in values.items()},
        )

        attempts: dict[str, int] = {}
        executed_irreversible = False
        pointer = 0
        # Set after a recovery that moves the pointer. The state that triggered
        # the recovery is still on screen at that moment, so re-inspecting it
        # before the jumped-to step has run would detect the same condition
        # again and spend the rule's remaining attempts on it.
        skip_preflight = False

        try:
            while pointer < len(capability.steps):
                try:
                    step = capability.steps[pointer]
                    self.recorder.event("step_started", step=step.index, intent=step.intent,
                                        action=step.action.kind, risk=step.risk)

                    # Before acting, see whether the application has already said
                    # something that changes what should happen -- an outcome, a
                    # nuisance to clear, or an error page.
                    if not skip_preflight:
                        pre = self._look(capability, step.index, expect=None)
                        if pre.reason == "app_failure":
                            raise _Blocked(self._application_failure(step, pre.require_signal()))
                        if pre.reason == "outcome":
                            return self._business(finish, pre.require_outcome(), step.index)
                        if pre.reason == "recovery":
                            jump = self._recover(capability, pre.require_recovery(), step, attempts,
                                                 executed_irreversible)
                            if jump is not None:
                                pointer, skip_preflight = jump, True
                            continue
                    skip_preflight = False

                    verdict = self.policy.check_step(
                        step, capability=capability, irreversible_authorised=authorise_irreversible)
                    if not verdict:
                        self.recorder.event("policy_blocked", step=step.index, intent=step.intent,
                                            risk=step.risk, reason=verdict.reason)
                        raise _Blocked(Failure(
                            kind=FailureKind.POLICY_BLOCKED,
                            step_index=step.index,
                            intent=step.intent,
                            expected="a step the guardrail permits",
                            observed=verdict.reason,
                        ))

                    self._perform(capability, step, values)
                    if step.risk == "irreversible":
                        executed_irreversible = True

                    # Wait for the step's expectation. A recovery here must not
                    # re-run the action: the action already happened, and repeating
                    # it would re-submit a form or click a control that is no longer
                    # on screen. Clearing the obstruction and waiting again is the
                    # whole of what recovery means at this point.
                    jumped = False
                    while True:
                        settled = self._wait(capability, step.index, step.expect, self.step_timeout_ms)
                        if settled.reason == "app_failure":
                            raise _Blocked(self._application_failure(step, settled.require_signal()))
                        if settled.reason == "outcome":
                            return self._business(finish, settled.require_outcome(), step.index)
                        if settled.reason == "recovery":
                            jump = self._recover(capability, settled.require_recovery(), step, attempts,
                                                 executed_irreversible)
                            if jump is not None:
                                pointer, skip_preflight, jumped = jump, True, True
                                break
                            continue
                        if settled.reason == "timeout":
                            # A step with no expectation settles immediately, so
                            # a timeout can only mean there was one.
                            assert step.expect is not None
                            raise _Blocked(
                                self._checkpoint_failure(step, step.expect, settled.observation))
                        break

                    if jumped:
                        continue

                    if step.expect is not None:
                        self.recorder.event("checkpoint_verified", step=step.index,
                                            condition=step.expect.description)
                    if self.step_pause_ms:
                        time.sleep(self.step_pause_ms / 1000)
                    completed += 1
                    pointer += 1
                except _Blocked as blocked:
                    resumed = self._escalate(capability, blocked.failure, step)
                    if resumed is None:
                        raise
                    pointer, granted = resumed
                    authorise_irreversible = authorise_irreversible or granted
                    skip_preflight = True
                    continue

            # The goal is not assumed from having run out of steps.
            final = self._wait(capability, len(capability.steps), capability.checkpoint,
                               self.step_timeout_ms)
            if final.reason == "app_failure":
                raise _Blocked(self._application_failure(capability.steps[-1], final.require_signal()))
            if final.reason == "outcome":
                return self._business(finish, final.require_outcome(), len(capability.steps))
            if final.reason != "expected":
                raise _Blocked(Failure(
                    kind=FailureKind.CHECKPOINT_FAILED,
                    step_index=len(capability.steps) - 1,
                    intent="reach the capability's success condition",
                    expected=capability.checkpoint.description,
                    observed="; ".join(
                        evaluate_condition(final.observation, capability.checkpoint).failed),
                ))
            self.recorder.event("checkpoint_verified", step=None,
                                condition=capability.checkpoint.description)

            outputs = self._extract(capability, final.observation)
            return finish(status=Status.SUCCESS, outputs=outputs)

        except _Blocked as blocked:
            failure = blocked.failure
            self.recorder.event("step_failed", step=failure.step_index,
                                failure=failure.kind.value,
                                expected=failure.expected, observed=failure.observed)
            # A failure is the one moment the richer evidence is worth its cost.
            self.recorder.screenshot(self.surface, f"failure-step-{failure.step_index}")
            self.recorder.snapshot(self.surface, f"failure-step-{failure.step_index}")
            # A run that asked for help and did not get it is reported as
            # escalated, not failed. The distinction matters to whoever reads
            # the queue in the morning: one of these is waiting for a person.
            if self._interventions:
                return finish(status=Status.ESCALATED, failure=failure)
            return finish(status=Status.FAILED, failure=failure)

    def _escalate(self, capability: Capability, failure: Failure, step: Step) -> tuple[int, bool] | None:
        """Bring a human onto the live session, and pick up where they leave it.

        The sequence is the whole of the control-transfer model:

          1. capture what the automation could see, before anything moves
          2. cede the token -- from here the engine cannot act, by construction
          3. raise a request carrying the goal, the step, why it stopped, the
             evidence, and a URL onto this very session
          4. wait
          5. observe what changed while somebody else was driving
          6. reclaim the token and resume where the operator left the screen

        Returns where to resume and whether the operator authorised the risky
        step, or None if nobody came -- in which case the run ends escalated
        with a complete request on file rather than pretending to have failed.
        """
        if self.escalator is None or failure.kind not in ESCALATABLE:
            return None

        asked = self._escalations_by_step.get(step.index, 0)
        if asked >= MAX_ESCALATIONS_PER_STEP:
            self.recorder.event("escalation_not_repeated", step=step.index,
                                already_asked=asked, failure=failure.kind.value,
                                reason="the step still cannot proceed after an operator "
                                       "handed it back; asking again would not change that")
            return None
        self._escalations_by_step[step.index] = asked + 1

        before = self.surface.observe()
        screenshot = self.recorder.screenshot(self.surface, f"escalation-step-{step.index}")
        snapshot = self.recorder.snapshot(self.surface, f"escalation-step-{step.index}")

        self.control.cede(f"{failure.kind.value} at step {step.index}")
        intervention = Intervention.create(
            run_id=self.recorder.run_id,
            capability_id=capability.id,
            capability_version=capability.version,
            goal=capability.description,
            step_index=step.index,
            step_intent=step.intent,
            reason=failure.observed,
            failure_kind=failure.kind.value,
            evidence_dir=str(self.recorder.directory),
            live_session_url=self.surface.live_session_url,
            screenshot=screenshot,
            snapshot=snapshot,
        )
        self.recorder.event("escalation_raised", step=step.index, request=intervention.request_id,
                            failure=failure.kind.value, reason=failure.observed,
                            live_session=intervention.live_session_url is not None)

        handback = self.escalator.escalate(intervention)

        # Observing was permitted throughout, which is what makes an
        # independent account of the operator's work possible at all.
        change = describe_change(before, self.surface.observe())
        self.control.reclaim("operator handed back" if handback else "nobody answered")
        self._interventions.append(intervention.request_id)

        if handback is None:
            self.recorder.event("escalation_unanswered", step=step.index,
                                request=intervention.request_id, observed_change=change)
            return None

        handback.observed_change = change
        self.recorder.event(
            "escalation_resolved", step=step.index, request=intervention.request_id,
            disposition=handback.disposition, operator=handback.operator,
            note=handback.note, observed_change=change,
            authorised_irreversible=handback.authorise_irreversible,
        )
        if handback.disposition != "resume":
            return None
        resume_at = step.index if handback.resume_from is None else handback.resume_from
        return resume_at, handback.authorise_irreversible

    def _application_failure(self, step: Step, signal: FailureSignal) -> Failure:
        return Failure(
            kind=FailureKind.APPLICATION_ERROR,
            step_index=step.index,
            intent=step.intent,
            expected="the application to respond normally",
            observed=f"{signal.code}: {signal.description}",
        )

    def _business(self, finish, outcome: BusinessOutcome, step_index: int) -> ReplayResult:
        """Report a legitimate answer that is not the goal.

        This is not an error path. The automation drove the application
        correctly and is passing on what it said, so nothing is captured as
        failure evidence and nothing is raised.
        """
        self.recorder.event("business_outcome", step=step_index, code=outcome.code,
                            description=outcome.description)
        return finish(status=Status.BUSINESS_OUTCOME, outcome_code=outcome.code,
                      outcome_description=outcome.description)
