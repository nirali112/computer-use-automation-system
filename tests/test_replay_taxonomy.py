"""The result contract, tested against a scripted surface.

These are the cases that matter most and are hardest to stage in a browser:
a recovery that runs out of attempts, a re-authentication refused because
something irreversible has already happened, an output that cannot be read
after the flow otherwise succeeded. Driving a fake surface makes each of them
a three-line setup instead of an elaborate fixture, and makes the assertions
about *which* failure was reported rather than about timing.
"""

import pytest

from cua.artifact import (
    AdjacentCell,
    Capability,
    Checkpoint,
    Click,
    Condition,
    Dismiss,
    Navigate,
    Output,
    Parameter,
    ParamValue,
    Provenance,
    Reauthenticate,
    Recovery,
    RoleName,
    Step,
    Surface as SurfaceSpec,
    Target,
    TextPresent,
    TypeText,
)
from cua.artifact.capability import BusinessOutcome, FailureSignal
from cua.evidence import Recorder
from cua.replay import FailureKind, ReplayEngine, Status
from cua.safety import Policy, permissive_for_testing
from cua.surfaces.base import Control, FrameView, Observation, Table


class ScriptedSurface:
    """A surface whose screen the test changes directly.

    Satisfies the same protocol the browser implementation does, which is the
    point: the engine cannot tell the difference, because it only ever sees
    an `Observation`.
    """

    def __init__(self, screen: Observation) -> None:
        self.screen = screen
        self.actions: list[str] = []

    def observe(self) -> Observation:
        return self.screen

    def navigate(self, url: str) -> None:
        self.actions.append(f"navigate {url}")

    def invoke(self, control: Control) -> None:
        self.actions.append(f"invoke {control.name or control.labels}")

    def enter_text(self, control: Control, text: str) -> None:
        self.actions.append(f"type into {control.name or control.labels}")

    def choose_option(self, control: Control, value: str) -> None:
        self.actions.append(f"choose {value}")

    def answer_dialog(self, accept: bool) -> None:
        self.actions.append(f"dialog accept={accept}")

    def screenshot(self) -> bytes:
        return b"\x89PNG"

    def snapshot(self) -> str:
        return "scripted surface"

    def close(self) -> None:
        pass


def screen(text: str, controls=(), tables=()) -> Observation:
    return Observation(
        url="http://x/", title="t",
        frames=(FrameView(name="mainFrame", url="http://x/", text=text),),
        controls=tuple(controls), tables=tuple(tables),
    )


def button(name: str) -> Control:
    return Control(frame="mainFrame", role="button", name=name, handle=name)


def by_name(role, name):
    return Target(description=f"the {name} control",
                  strategies=[RoleName(role=role, name=name, confidence="high", rationale="r")])


def capability(**overrides) -> Capability:
    base = dict(
        id="probe",
        name="probe",
        description="a capability under test",
        surface=SurfaceSpec(application="Meridian Core", entry_point="http://x/"),
        steps=[Step(index=0, intent="open the console", action=Navigate(url="http://x/"))],
        checkpoint=Checkpoint(description="the goal screen", assertions=[TextPresent(text="Done")]),
        provenance=Provenance(recorded_by="test", run_id="r"),
    )
    base.update(overrides)
    return Capability(**base)


OPEN = permissive_for_testing("http://x")


def run(surface, cap, inputs=None, tmp_path=None, policy=OPEN, **run_kw):
    recorder = Recorder("probe", tmp_path or "/tmp/cua-taxonomy", secrets=set())
    engine = ReplayEngine(surface, recorder, policy, step_timeout_ms=300, poll_ms=50)
    return engine.run(cap, inputs or {}, **run_kw)


# -- success and business outcomes are both successful replays -------------

def test_success_returns_the_declared_outputs_typed(tmp_path):
    surface = ScriptedSurface(screen("Done", tables=[
        Table(frame="mainFrame", headers=(), rows=(("Name", "Dana Whitfield"), ("Balance", "$4,182.55")))]))
    cap = capability(outputs=[
        Output(name="member_name", type="string", description="d", extract=AdjacentCell(label_text="Name")),
        Output(name="balance", type="number", description="d",
               extract=AdjacentCell(label_text="Balance"), pattern=r"\$([\d,]+\.\d{2})"),
    ])
    result = run(surface, cap, tmp_path=tmp_path)
    assert result.status is Status.SUCCESS
    assert result.outputs == {"member_name": "Dana Whitfield", "balance": 4182.55}


def test_a_business_outcome_is_a_successful_replay(tmp_path):
    """The distinction the whole system turns on. The automation worked; the
    application's answer simply was not the goal."""
    surface = ScriptedSurface(screen("No record found for member ID 999999."))
    cap = capability(business_outcomes=[BusinessOutcome(
        code="MEMBER_NOT_FOUND", description="no such member",
        detect=Condition(description="d", assertions=[TextPresent(text="No record found")]))])
    result = run(surface, cap, tmp_path=tmp_path)
    assert result.status is Status.BUSINESS_OUTCOME
    assert result.outcome_code == "MEMBER_NOT_FOUND"
    assert result.replay_worked is True
    assert result.failure is None


def test_an_outcome_declared_after_a_step_is_not_detected_before_it(tmp_path):
    """Otherwise a phrase that appears throughout a flow would end it early."""
    surface = ScriptedSurface(screen("No record found"))
    cap = capability(
        steps=[Step(index=0, intent="open", action=Navigate(url="http://x/")),
               Step(index=1, intent="search", action=Navigate(url="http://x/s"))],
        checkpoint=Checkpoint(description="c", assertions=[TextPresent(text="No record found")]),
        business_outcomes=[BusinessOutcome(
            code="LATE", description="only after searching", after_step=1,
            detect=Condition(description="d", assertions=[TextPresent(text="No record found")]))])
    result = run(surface, cap, tmp_path=tmp_path)
    assert result.status is Status.BUSINESS_OUTCOME
    assert result.steps_completed == 1, "the outcome should not have fired at step 0"


# -- failures name what went wrong -----------------------------------------

def test_invalid_input_is_rejected_before_the_surface_is_touched(tmp_path):
    surface = ScriptedSurface(screen("Done"))
    cap = capability(parameters=[Parameter(name="member_id", type="string", description="d",
                                           pattern=r"^\d{6}$")])
    result = run(surface, cap, {"member_id": "12"}, tmp_path=tmp_path)
    assert result.status is Status.FAILED
    assert result.failure.kind is FailureKind.INVALID_INPUT
    assert surface.actions == [], "a malformed call should cost nothing"


def test_a_rejected_value_is_not_quoted_back_into_the_failure(tmp_path):
    """A rejected password is still a password, and this text reaches the log."""
    surface = ScriptedSurface(screen("Done"))
    cap = capability(parameters=[Parameter(name="pin", type="string", description="d",
                                           sensitive=True, pattern=r"^\d{4}$")])
    result = run(surface, cap, {"pin": "hunter2"}, tmp_path=tmp_path)
    assert "hunter2" not in result.failure.observed


def test_an_unresolvable_target_reports_every_strategy_tried(tmp_path):
    surface = ScriptedSurface(screen("Sign On", controls=[button("Cancel")]))
    cap = capability(steps=[Step(index=0, intent="sign on", action=Click(target=by_name("button", "Sign On")))])
    result = run(surface, cap, tmp_path=tmp_path)
    assert result.failure.kind is FailureKind.TARGET_UNRESOLVED
    assert "role_name" in result.failure.observed


def test_an_ambiguous_target_fails_rather_than_choosing(tmp_path):
    surface = ScriptedSurface(screen("Results", controls=[button("Open"), button("Open")]))
    cap = capability(steps=[Step(index=0, intent="open the record",
                                 action=Click(target=by_name("button", "Open")))])
    result = run(surface, cap, tmp_path=tmp_path)
    assert result.failure.kind is FailureKind.TARGET_UNRESOLVED
    assert "refusing to guess" in result.failure.observed


def test_a_missed_checkpoint_names_the_assertions_that_did_not_hold(tmp_path):
    surface = ScriptedSurface(screen("Sign On"))
    result = run(ScriptedSurface(screen("Sign On")), capability(), tmp_path=tmp_path)
    assert result.failure.kind is FailureKind.CHECKPOINT_FAILED
    assert "Done" in result.failure.observed


def test_a_declared_application_error_is_reported_as_one_immediately(tmp_path):
    """Without the declaration this would surface as a checkpoint timeout,
    which is both slower and blames the wrong thing."""
    surface = ScriptedSurface(screen("Application Error  Reference: ERR-1"))
    cap = capability(failure_signals=[FailureSignal(
        code="CONSOLE_ERROR", description="the console fell over",
        detect=Condition(description="d", assertions=[TextPresent(text="Application Error")]))])
    result = run(surface, cap, tmp_path=tmp_path)
    assert result.failure.kind is FailureKind.APPLICATION_ERROR
    assert "CONSOLE_ERROR" in result.failure.observed
    assert result.duration_ms < 300, "an error page should not be waited out"


def test_an_unreadable_output_is_distinct_from_a_missed_checkpoint(tmp_path):
    """The automation drove correctly and the contract is still unmet."""
    surface = ScriptedSurface(screen("Done"))
    cap = capability(outputs=[Output(name="balance", type="number", description="d",
                                     extract=AdjacentCell(label_text="Balance"))])
    result = run(surface, cap, tmp_path=tmp_path)
    assert result.failure.kind is FailureKind.EXTRACTION_FAILED


# -- recovery is bounded ---------------------------------------------------

def test_a_recurring_condition_exhausts_its_attempts_and_fails(tmp_path):
    """The notice is never cleared, because the scripted screen never changes.
    Bounded recovery is what stops that becoming an infinite loop."""
    surface = ScriptedSurface(screen("System Notice", controls=[button("Continue")]))
    cap = capability(recovery=[Recovery(
        name="dismiss_notice", description="a notice", max_attempts=2,
        detect=Condition(description="d", assertions=[TextPresent(text="System Notice")]),
        remedy=Dismiss(target=by_name("button", "Continue")))])
    result = run(surface, cap, tmp_path=tmp_path)
    assert result.failure.kind is FailureKind.RECOVERY_EXHAUSTED
    assert len(result.recoveries) == 2


def test_reauthentication_is_refused_once_something_irreversible_has_run(tmp_path):
    """Replaying the flow would submit the request a second time. A duplicated
    transaction is far worse than a failed replay, so this refuses."""
    surface = ScriptedSurface(screen("session has expired", controls=[button("Submit Request")]))
    cap = capability(
        approval="approved",
        steps=[Step(index=0, intent="submit the request", risk="irreversible",
                    action=Click(target=by_name("button", "Submit Request")))],
        recovery=[Recovery(
            name="reauthenticate", description="the session expired", max_attempts=2,
            detect=Condition(description="d", assertions=[TextPresent(text="session has expired")]),
            remedy=Reauthenticate(restart_from=0))])
    result = run(surface, cap, tmp_path=tmp_path, authorise_irreversible=True)
    assert result.failure.kind is FailureKind.UNSAFE_TO_RECOVER
    assert "second time" in result.failure.observed


def test_recovery_after_an_action_does_not_repeat_the_action(tmp_path):
    """Repeating it would re-submit a form or click a control that has since
    left the screen. Clearing the obstruction and waiting again is the whole
    of what recovery means at that point."""
    surface = ScriptedSurface(screen("Sign On", controls=[button("Sign On")]))
    original_invoke = surface.invoke

    def invoke(control):
        original_invoke(control)
        if control.name == "Sign On":
            # The console interposes the notice in response to the click.
            surface.screen = screen("System Notice", controls=[button("Continue")])
        elif control.name == "Continue":
            surface.screen = screen("Done")

    surface.invoke = invoke

    cap = capability(
        steps=[Step(index=0, intent="sign on", action=Click(target=by_name("button", "Sign On")),
                    expect=Checkpoint(description="the goal screen",
                                      assertions=[TextPresent(text="Done")]))],
        recovery=[Recovery(
            name="dismiss_notice", description="a notice",
            detect=Condition(description="d", assertions=[TextPresent(text="System Notice")]),
            remedy=Dismiss(target=by_name("button", "Continue")))])
    result = run(surface, cap, tmp_path=tmp_path)
    assert result.status is Status.SUCCESS
    assert surface.actions.count("invoke Sign On") == 1


# -- evidence --------------------------------------------------------------

def test_sensitive_arguments_are_named_but_never_written(tmp_path):
    surface = ScriptedSurface(screen("Done"))
    cap = capability(parameters=[
        Parameter(name="operator", type="string", description="d", sensitive=True),
        Parameter(name="member_id", type="string", description="d"),
    ])
    recorder = Recorder("probe", tmp_path, secrets={"Passw0rd!"})
    ReplayEngine(surface, recorder, OPEN, step_timeout_ms=300, poll_ms=50).run(
        cap, {"operator": "Passw0rd!", "member_id": "100234"})
    written = (tmp_path / "probe" / "events.jsonl").read_text()
    assert "Passw0rd!" not in written
    assert "operator" in written and "100234" in written


def test_a_sensitive_output_reaches_the_caller_but_not_the_log(tmp_path):
    surface = ScriptedSurface(screen("Done", tables=[
        Table(frame="mainFrame", headers=(), rows=(("Name", "Dana Whitfield"),))]))
    cap = capability(outputs=[Output(name="member_name", type="string", description="d",
                                     sensitive=True, extract=AdjacentCell(label_text="Name"))])
    recorder = Recorder("probe", tmp_path, secrets=set())
    result = ReplayEngine(surface, recorder, OPEN, step_timeout_ms=300, poll_ms=50).run(cap, {})
    assert result.outputs == {"member_name": "Dana Whitfield"}
    assert "Dana Whitfield" not in (tmp_path / "probe" / "events.jsonl").read_text()
    assert "Dana Whitfield" not in (tmp_path / "probe" / "result.json").read_text()


def test_failure_evidence_is_captured_only_when_something_fails(tmp_path):
    run(ScriptedSurface(screen("Sign On")), capability(), tmp_path=tmp_path / "bad")
    run(ScriptedSurface(screen("Done")), capability(), tmp_path=tmp_path / "good")
    assert list((tmp_path / "bad" / "probe").glob("failure-*"))
    assert not list((tmp_path / "good" / "probe").glob("failure-*"))


def test_an_event_field_may_not_shadow_the_event_kind(tmp_path):
    """Caught in the wild: an event carrying its own kind= silently overwrote
    the event type, so step_failed was logged as application_error and
    disappeared from the log. Refusing loudly is the only fix that stays."""
    recorder = Recorder("probe", tmp_path)
    with pytest.raises(ValueError, match="shadow the reserved event keys"):
        recorder.event("step_failed", kind="application_error")
