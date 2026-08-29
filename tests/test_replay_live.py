"""Replay against the real application, in a real browser.

The taxonomy is tested exhaustively elsewhere against a scripted surface. What
this establishes is that the same engine, driving actual Chromium against the
actual console, classifies real behaviour the same way -- that the boundary
between "the application answered" and "the automation broke" survives contact
with framesets, redirects, sessions and unlabelled forms.
"""


import pytest

from cua.evidence import Recorder
from cua.replay import FailureKind, ReplayEngine, Status
from cua.safety import permissive_for_testing
from cua.surfaces.web import WebSurface
from mockbank.faults import FAULTS
from reference_capability import member_balance_capability

CREDENTIALS = {"operator_id": "teller01", "operator_password": "Passw0rd!"}


@pytest.fixture
def replay(base_url, tmp_path):
    """Runs the reference capability against the live console."""
    opened: list[WebSurface] = []

    def go(label: str, **inputs):
        capability = member_balance_capability()
        for step in capability.steps:
            if step.action.kind == "navigate":
                step.action.url = f"{base_url}/"
        recorder = Recorder(label, tmp_path, secrets=set(CREDENTIALS.values()))
        surface = WebSurface()
        opened.append(surface)
        engine = ReplayEngine(surface, recorder, permissive_for_testing(base_url),
                              step_timeout_ms=8_000)
        return engine.run(capability, inputs), recorder

    yield go
    for surface in opened:
        surface.close()


# -- the two successful shapes ---------------------------------------------

def test_a_recorded_flow_replays_and_returns_typed_outputs(replay):
    result, _ = replay("success", member_id="100234", **CREDENTIALS)
    assert result.status is Status.SUCCESS
    assert result.outputs == {"member_name": "Dana Whitfield", "savings_balance": 4182.55}
    assert result.steps_completed == 8


def test_an_unknown_member_is_reported_as_an_outcome_not_a_failure(replay):
    """The case the brief singles out as most often got wrong."""
    result, _ = replay("not-found", member_id="999999", **CREDENTIALS)
    assert result.status is Status.BUSINESS_OUTCOME
    assert result.outcome_code == "MEMBER_NOT_FOUND"
    assert result.replay_worked and result.failure is None


def test_rejected_credentials_are_reported_as_their_own_outcome(replay):
    result, _ = replay("signon-failed", member_id="100234",
                       operator_id="teller01", operator_password="not-the-password")
    assert result.status is Status.BUSINESS_OUTCOME
    assert result.outcome_code == "SIGNON_FAILED"


# -- conditions replay resolves by itself ----------------------------------

def test_an_unexpected_interstitial_is_cleared_and_the_flow_completes(replay):
    FAULTS.arm("interstitial")
    result, _ = replay("interstitial", member_id="100234", **CREDENTIALS)
    assert result.status is Status.SUCCESS
    assert [r.rule for r in result.recoveries] == ["dismiss_maintenance_notice"]


def test_an_expired_session_is_re_established_and_the_flow_restarts(replay):
    FAULTS.arm("session_timeout")
    result, _ = replay("session-expiry", member_id="100234", **CREDENTIALS)
    assert result.status is Status.SUCCESS
    assert [r.rule for r in result.recoveries] == ["reauthenticate_expired_session"]
    assert result.steps_completed > 8, "the flow should have replayed from the start"


def test_a_transient_stall_is_waited_out_rather_than_failing(replay):
    """Waiting on a condition rather than a duration is what makes this work:
    the step completes when the screen is ready, however long that took."""
    FAULTS.arm("slow")
    result, _ = replay("slow", member_id="100234", **CREDENTIALS)
    assert result.status is Status.SUCCESS


# -- conditions replay must not resolve by itself --------------------------

def test_an_application_error_is_reported_as_one_and_not_waited_out(replay):
    FAULTS.arm("app_error")
    result, _ = replay("app-error", member_id="100234", **CREDENTIALS)
    assert result.status is Status.FAILED
    assert result.failure.kind is FailureKind.APPLICATION_ERROR
    assert "CONSOLE_ERROR" in result.failure.observed
    assert result.duration_ms < 8_000, "a declared error should not be waited out"


def test_a_failure_leaves_evidence_that_explains_itself(replay):
    FAULTS.arm("app_error")
    result, recorder = replay("app-error-evidence", member_id="100234", **CREDENTIALS)
    assert result.status is Status.FAILED
    assert list(recorder.directory.glob("failure-*.png"))
    assert list(recorder.directory.glob("failure-*.txt"))
    kinds = [event["kind"] for event in recorder.events()]
    assert "step_failed" in kinds and "run_finished" in kinds


# -- evidence discipline ---------------------------------------------------

def test_no_credential_or_member_name_appears_anywhere_in_the_evidence(replay):
    result, recorder = replay("redaction", member_id="100234", **CREDENTIALS)
    assert result.outputs["member_name"] == "Dana Whitfield"
    for path in recorder.directory.rglob("*"):
        if path.suffix in {".jsonl", ".json", ".txt"}:
            written = path.read_text()
            assert "Passw0rd!" not in written
            assert "teller01" not in written
            assert "Dana Whitfield" not in written, f"member name leaked into {path.name}"


def test_the_log_records_which_strategy_resolved_each_control(replay):
    """A capability quietly surviving on its last-resort strategy should be
    visible in the evidence, not silent."""
    _, recorder = replay("strategies", member_id="100234", **CREDENTIALS)
    resolved = [e for e in recorder.events() if e["kind"] == "target_resolved"]
    assert resolved
    assert all(event["strategy"] and event["confidence"] for event in resolved)
    assert any(event["attempts"] for event in resolved)


# -- handing the live session to a person ----------------------------------

def test_an_irreversible_step_stops_for_a_person_who_authorises_it_on_the_session(
        base_url, tmp_path):
    """The complete loop, against the real console.

    A write flow is blocked at its submission, a request is raised carrying a
    link into the session the automation was already using, the automation is
    structurally unable to act while the operator holds it, and the run
    resumes and completes once they hand it back with an authorisation.
    """
    from cua.escalation import ControlViolation, Handback, Holder, SessionControl
    from cua.safety import Policy
    from reference_capability import subaccount_capability

    capability = subaccount_capability()
    for step in capability.steps:
        if step.action.kind == "navigate":
            step.action.url = f"{base_url}/"

    # Irreversible actions are permitted by policy; the caller deliberately
    # does not authorise them, so only a person can unblock this.
    policy = Policy(allowed_origins=[base_url], allowed_paths=["*"], allow_irreversible=True)
    control = SessionControl()
    seen = {}

    class Operator:
        def escalate(self, intervention):
            seen["request"] = intervention
            seen["holder"] = control.holder
            try:
                engine.surface.navigate(base_url)
                seen["automation_could_act"] = True
            except ControlViolation:
                seen["automation_could_act"] = False
            return Handback(disposition="resume", operator="j.okafor",
                            note="Reviewed against the member's file; authorising.",
                            authorise_irreversible=True)

    surface = WebSurface()
    recorder = Recorder("handoff", tmp_path, secrets=set(CREDENTIALS.values()))
    engine = ReplayEngine(surface, recorder, policy, escalator=Operator(), control=control,
                          step_timeout_ms=8_000)
    try:
        result = engine.run(capability, dict(
            member_id="100234", product="Vacation Club", opening_deposit="150.00",
            nickname="Summer Trip", **CREDENTIALS))
    finally:
        surface.close()

    request = seen["request"]
    assert request.failure_kind == "policy_blocked"
    assert request.step_intent == "submit the sub-account request"
    assert request.live_session_url and "/devtools/inspector.html" in request.live_session_url
    assert seen["holder"] is Holder.HUMAN
    assert seen["automation_could_act"] is False, "the automation acted during a handoff"

    assert result.status is Status.SUCCESS
    assert result.outputs["confirmation_number"].startswith("CNF-")
    assert result.outputs["account_number"].startswith("SAV-100234-")
    assert result.interventions
    assert control.holder is Holder.AUTOMATION

    resolved = [e for e in recorder.events() if e["kind"] == "escalation_resolved"]
    assert resolved and resolved[0]["authorised_irreversible"] is True


def test_a_restricted_member_is_a_business_outcome_not_an_escalation(base_url, tmp_path):
    """A permission denial is the institution's decision, already made. There
    is nothing for an operator to resolve, so nobody is called."""
    from cua.safety import permissive_for_testing
    from reference_capability import subaccount_capability

    capability = subaccount_capability()
    for step in capability.steps:
        if step.action.kind == "navigate":
            step.action.url = f"{base_url}/"

    surface = WebSurface()
    try:
        result = ReplayEngine(surface, Recorder("denied", tmp_path),
                              permissive_for_testing(base_url), step_timeout_ms=8_000).run(
            capability, dict(member_id="100999", product="Regular Savings",
                             opening_deposit="100.00", nickname="Rainy Day", **CREDENTIALS),
            authorise_irreversible=True)
    finally:
        surface.close()

    assert result.status is Status.BUSINESS_OUTCOME
    assert result.outcome_code == "SERVICING_NOT_PERMITTED"
    assert result.interventions == []
