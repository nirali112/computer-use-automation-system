"""Bringing a human onto the live session, and getting it back.

The brief is explicit that this must not be a TODO, and the part that makes it
real is not the request format -- it is that the operator drives the same
session, and that the automation cannot act while they do. Both are asserted
here, the second by trying to violate it.
"""

import pytest

from cua.escalation import (
    ControlledSurface,
    ControlViolation,
    Handback,
    Holder,
    Intervention,
    InterventionQueue,
    QueueEscalator,
    RecordOnlyEscalator,
    SessionControl,
    describe_change,
)
from cua.evidence import Recorder
from cua.replay import FailureKind, ReplayEngine, Status
from cua.safety import Policy
from cua.surfaces.base import Control, FrameView, Observation

from test_replay_taxonomy import (
    ScriptedSurface,
    button,
    by_name,
    capability,
    screen,
)
from cua.artifact import Click, Navigate, Step

OPEN = Policy(allowed_origins=["http://x"], allowed_paths=["*"])


# -- who is driving --------------------------------------------------------

def test_control_starts_with_the_automation_and_records_every_handover():
    control = SessionControl()
    assert control.holder is Holder.AUTOMATION and control.automation_may_act
    control.cede("stuck at step 6")
    assert control.holder is Holder.HUMAN and not control.automation_may_act
    control.reclaim("operator finished")
    assert [(t.to.value, t.reason) for t in control.transfers] == [
        ("human", "stuck at step 6"), ("automation", "operator finished")]


def test_control_cannot_be_handed_to_whoever_already_holds_it():
    """Two sides politely taking turns is not a control model."""
    control = SessionControl()
    with pytest.raises(ControlViolation):
        control.reclaim("already mine")


def test_the_automation_cannot_act_while_a_human_holds_the_session():
    control = SessionControl()
    guarded = ControlledSurface(ScriptedSurface(screen("Form", controls=[button("Submit")])), control)
    control.cede("operator taking over")
    with pytest.raises(ControlViolation, match="held by human"):
        guarded.invoke(button("Submit"))
    with pytest.raises(ControlViolation):
        guarded.navigate("http://x/")
    with pytest.raises(ControlViolation):
        guarded.enter_text(button("Submit"), "text")


def test_the_automation_may_still_watch_while_a_human_works():
    """Watching is not acting. Blinding the automation during a handoff would
    make an independent record of what the operator did impossible."""
    control = SessionControl()
    guarded = ControlledSurface(ScriptedSurface(screen("Form")), control)
    control.cede("operator taking over")
    assert guarded.observe().text_in("mainFrame") == "Form"
    assert guarded.snapshot()
    assert guarded.screenshot()


def test_acting_is_permitted_again_once_control_comes_back():
    control = SessionControl()
    inner = ScriptedSurface(screen("Form", controls=[button("Submit")]))
    guarded = ControlledSurface(inner, control)
    control.cede("over to you")
    control.reclaim("thanks")
    guarded.invoke(button("Submit"))
    assert inner.actions == ["invoke Submit"]


# -- the independent record of what the human did --------------------------

def test_a_change_of_screen_is_described_without_the_operator_saying_so():
    """An operator's own account is worth having and is also, on a bad day,
    incomplete. This is the record that does not depend on it."""
    before = Observation(url="http://x/a", title="t",
                         frames=(FrameView("mainFrame", "http://x/a", "Sign On"),))
    after = Observation(url="http://x/b", title="t",
                        frames=(FrameView("mainFrame", "http://x/b", "Member Detail"),),
                        controls=(Control(frame="mainFrame", role="link", name="Open Sub-Account"),))
    described = describe_change(before, after)
    assert "navigated from http://x/a to http://x/b" in described
    assert "content of frame 'mainFrame' changed" in described
    assert "Open Sub-Account" in described


def test_an_unchanged_session_is_reported_as_unchanged():
    same = Observation(url="http://x/", title="t", frames=(FrameView("mainFrame", "http://x/", "Sign On"),))
    assert describe_change(same, same) == "no observable change to the session"


# -- the queue -------------------------------------------------------------

def intervention(**kw) -> Intervention:
    base = dict(run_id="r", capability_id="probe", capability_version=1, goal="g",
                step_index=3, step_intent="submit", reason="blocked",
                failure_kind="policy_blocked", evidence_dir="/tmp")
    base.update(kw)
    return Intervention.create(**base)


def test_a_raised_request_is_readable_by_somebody_who_was_not_watching(tmp_path):
    queue = InterventionQueue(tmp_path)
    raised = intervention(live_session_url="http://127.0.0.1:9/devtools/inspector.html")
    queue.raise_request(raised)
    [pending] = queue.pending()
    assert pending.request_id == raised.request_id
    assert pending.step_intent == "submit" and pending.live_session_url
    assert pending.state == "pending"


def test_claiming_a_request_stops_two_operators_driving_one_session(tmp_path):
    queue = InterventionQueue(tmp_path)
    raised = intervention()
    queue.raise_request(raised)
    queue.claim(raised.request_id, "j.okafor")
    with pytest.raises(RuntimeError, match="already claimed"):
        queue.claim(raised.request_id, "someone.else")


def test_a_handback_resolves_the_request(tmp_path):
    queue = InterventionQueue(tmp_path)
    raised = intervention()
    queue.raise_request(raised)
    queue.hand_back(raised.request_id, Handback(disposition="resume", operator="j.okafor",
                                                note="authorised"))
    assert queue.pending() == []
    answer = queue.read_handback(raised.request_id)
    assert answer.disposition == "resume" and answer.operator == "j.okafor"


def test_an_escalator_that_does_not_wait_still_leaves_a_complete_request(tmp_path):
    """For unattended runs with nobody on shift: end escalated with a request
    on file, rather than blocking a worker for an hour on the chance."""
    queue = InterventionQueue(tmp_path)
    assert RecordOnlyEscalator(queue).escalate(intervention()) is None
    assert len(queue.pending()) == 1


def test_an_escalator_that_waits_gives_up_rather_than_hanging_forever(tmp_path):
    queue = InterventionQueue(tmp_path)
    escalator = QueueEscalator(queue, timeout_s=0.3, poll_s=0.05)
    assert escalator.escalate(intervention()) is None


# -- the engine's side of it ----------------------------------------------

class StubOperator:
    """Answers an intervention however the test needs."""

    def __init__(self, handback=None, on_call=None):
        self.handback = handback
        self.on_call = on_call
        self.seen = None

    def escalate(self, intervention):
        self.seen = intervention
        if self.on_call:
            self.on_call(intervention)
        return self.handback


def stuck_capability():
    return capability(steps=[Step(index=0, intent="sign on",
                                  action=Click(target=by_name("button", "Sign On")))])


def test_a_stuck_run_raises_a_request_carrying_enough_to_act_on(tmp_path):
    operator = StubOperator()
    engine = ReplayEngine(ScriptedSurface(screen("Nothing here")), Recorder("r", tmp_path), OPEN,
                          escalator=operator, step_timeout_ms=200, poll_ms=50)
    result = engine.run(stuck_capability(), {})
    raised = operator.seen
    assert raised.capability_id == "probe"
    assert raised.step_index == 0 and raised.step_intent == "sign on"
    assert raised.failure_kind == FailureKind.TARGET_UNRESOLVED.value
    assert "role_name" in raised.reason
    assert raised.evidence_dir and raised.screenshot and raised.snapshot
    assert result.status is Status.ESCALATED


def test_control_is_ceded_for_the_handoff_and_taken_back_afterwards(tmp_path):
    control = SessionControl()
    holders = []
    operator = StubOperator(on_call=lambda _: holders.append(control.holder))
    ReplayEngine(ScriptedSurface(screen("Nothing here")), Recorder("r", tmp_path), OPEN,
                 escalator=operator, control=control, step_timeout_ms=200, poll_ms=50
                 ).run(stuck_capability(), {})
    assert holders == [Holder.HUMAN], "the operator must hold the session while working"
    assert control.holder is Holder.AUTOMATION, "control must come back"


def test_a_resumed_run_carries_on_from_where_the_operator_left_it(tmp_path):
    surface = ScriptedSurface(screen("Nothing here"))

    def operator_fixes_it(_):
        # Standing at the session, the operator gets the console to the screen
        # the automation was expecting.
        surface.screen = screen("Done", controls=[button("Sign On")])

    operator = StubOperator(handback=Handback(disposition="resume", operator="j.okafor",
                                              note="navigated there manually"),
                            on_call=operator_fixes_it)
    result = ReplayEngine(surface, Recorder("r", tmp_path), OPEN, escalator=operator,
                          step_timeout_ms=200, poll_ms=50).run(stuck_capability(), {})
    assert result.status is Status.SUCCESS
    assert result.interventions


def test_an_abandoned_request_ends_the_run_escalated_not_failed(tmp_path):
    """The distinction matters to whoever reads the queue in the morning."""
    operator = StubOperator(handback=Handback(disposition="abandon", operator="j.okafor",
                                              note="needs a change to the capability"))
    result = ReplayEngine(ScriptedSurface(screen("Nothing here")), Recorder("r", tmp_path), OPEN,
                          escalator=operator, step_timeout_ms=200, poll_ms=50
                          ).run(stuck_capability(), {})
    assert result.status is Status.ESCALATED
    assert result.failure is not None, "the reason it stopped is still reported"


def test_what_the_operator_did_is_recorded_alongside_what_they_say_they_did(tmp_path):
    surface = ScriptedSurface(screen("Nothing here"))
    operator = StubOperator(
        handback=Handback(disposition="resume", operator="j.okafor", note="opened it by hand"),
        on_call=lambda _: setattr(surface, "screen", screen("Done", controls=[button("Sign On")])))
    recorder = Recorder("r", tmp_path)
    ReplayEngine(surface, recorder, OPEN, escalator=operator,
                 step_timeout_ms=200, poll_ms=50).run(stuck_capability(), {})
    [resolved] = [e for e in recorder.events() if e["kind"] == "escalation_resolved"]
    assert resolved["note"] == "opened it by hand"
    assert "changed" in resolved["observed_change"]


def test_a_callers_own_mistake_is_not_escalated_to_an_operator(tmp_path):
    """Escalating what no operator can fix trains them to dismiss requests,
    which is how a working escalation path quietly stops working."""
    from cua.artifact import Parameter

    operator = StubOperator()
    cap = capability(parameters=[Parameter(name="member_id", type="string", description="d",
                                           pattern=r"^\d{6}$")])
    result = ReplayEngine(ScriptedSurface(screen("Done")), Recorder("r", tmp_path), OPEN,
                          escalator=operator, step_timeout_ms=200, poll_ms=50
                          ).run(cap, {"member_id": "12"})
    assert result.status is Status.FAILED
    assert result.failure.kind is FailureKind.INVALID_INPUT
    assert operator.seen is None


def test_a_step_is_not_escalated_twice(tmp_path):
    """Bounded for the same reason recovery is.

    An operator who authorises a step that a different gate then blocks would
    otherwise be asked again, and again, with the run making no progress
    between requests. The second refusal ends the run instead.
    """
    calls = []

    class RepeatOperator:
        def escalate(self, intervention):
            calls.append(intervention.request_id)
            # Resumes the same step, which will fail the same way.
            return Handback(disposition="resume", operator="j.okafor", note="try again")

    result = ReplayEngine(ScriptedSurface(screen("Nothing here")), Recorder("r", tmp_path), OPEN,
                          escalator=RepeatOperator(), step_timeout_ms=200, poll_ms=50
                          ).run(stuck_capability(), {})
    assert len(calls) == 1, "the same step asked for a person more than once"
    assert result.status is Status.ESCALATED
