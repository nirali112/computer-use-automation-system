"""Guardrails, and the discipline about what gets written down.

Two separate claims are tested here, because they fail in different ways. The
allowlist is about what the automation is permitted to do, and a gap in it
shows up as an action nobody sanctioned. Redaction is about what survives in
writing, and a gap in it shows up months later in a log nobody thought about.
"""


from cua.artifact import (
    Capability,
    Checkpoint,
    Click,
    Navigate,
    Provenance,
    Step,
    Surface as SurfaceSpec,
    TextPresent,
)
from cua.evidence import Recorder
from cua.replay import FailureKind, ReplayEngine, Status
from cua.safety import Policy, REDACTED, Redactor

from test_replay_taxonomy import ScriptedSurface, by_name, screen


def capability(**overrides) -> Capability:
    base = dict(
        id="probe", name="probe", description="d",
        surface=SurfaceSpec(application="Meridian Core", entry_point="http://bank.test/"),
        steps=[Step(index=0, intent="open", action=Navigate(url="http://bank.test/search"))],
        checkpoint=Checkpoint(description="c", assertions=[TextPresent(text="Done")]),
        provenance=Provenance(recorded_by="test", run_id="r"),
    )
    base.update(overrides)
    return Capability(**base)


BANK_ONLY = Policy(allowed_origins=["http://bank.test"], allowed_paths=["/", "/search", "/member/*"])


# -- where the automation may act ------------------------------------------

def test_an_off_allowlist_origin_is_refused():
    verdict = BANK_ONLY.check_navigation("https://evil.example.com/")
    assert not verdict and "not on the allowlist" in verdict.reason


def test_a_permitted_origin_with_an_unpermitted_route_is_refused():
    """Origin alone is too coarse: reading a member and administering the
    institution live on the same host."""
    verdict = BANK_ONLY.check_navigation("http://bank.test/admin/institution")
    assert not verdict and "does not match any permitted route" in verdict.reason


def test_a_permitted_route_pattern_is_allowed():
    assert BANK_ONLY.check_navigation("http://bank.test/member/100234")


# -- what it may do --------------------------------------------------------

def test_an_action_type_outside_the_policy_is_refused():
    policy = Policy(allowed_origins=["http://bank.test"], allowed_actions=["navigate"])
    step = Step(index=0, intent="click", action=Click(target=by_name("button", "Submit")))
    verdict = policy.check_step(step, capability=capability())
    assert not verdict and "not permitted by policy" in verdict.reason


def test_an_irreversible_step_is_blocked_by_default():
    """A blocked transfer is an inconvenience resolved in minutes; an
    unintended one is money that has moved. The default belongs on the side
    that is recoverable."""
    step = Step(index=0, intent="submit", risk="irreversible",
                action=Click(target=by_name("button", "Submit Request")))
    verdict = BANK_ONLY.check_step(step, capability=capability(approval="approved"),
                                   irreversible_authorised=True)
    assert not verdict and "does not permit irreversible actions" in verdict.reason


def test_an_irreversible_step_needs_authorisation_even_when_policy_allows_it():
    """Per invocation rather than a stored setting, so the decision is made by
    whoever is asking, at the moment of asking."""
    policy = BANK_ONLY.model_copy(update={"allow_irreversible": True})
    step = Step(index=0, intent="submit", risk="irreversible",
                action=Click(target=by_name("button", "Submit Request")))
    assert not policy.check_step(step, capability=capability(approval="approved"))
    assert policy.check_step(step, capability=capability(approval="approved"),
                             irreversible_authorised=True)


def test_an_unapproved_capability_may_not_take_irreversible_actions():
    """A capability that happened to work once during discovery should not be
    able to move money before a human has read what it does."""
    policy = BANK_ONLY.model_copy(update={"allow_irreversible": True})
    step = Step(index=0, intent="submit", risk="irreversible",
                action=Click(target=by_name("button", "Submit Request")))
    verdict = policy.check_step(step, capability=capability(approval="draft"),
                                irreversible_authorised=True)
    assert not verdict and "requires an approved capability" in verdict.reason


def test_a_safe_step_is_unaffected_by_the_irreversible_gates():
    step = Step(index=0, intent="search", action=Navigate(url="http://bank.test/search"))
    assert BANK_ONLY.check_step(step, capability=capability())


# -- enforcement is on every step, not once at the start -------------------

def test_the_engine_refuses_a_step_the_policy_blocks(tmp_path):
    surface = ScriptedSurface(screen("Done"))
    cap = capability(steps=[Step(index=0, intent="leave the allowlist",
                                 action=Navigate(url="https://evil.example.com/"))])
    engine = ReplayEngine(surface, Recorder("probe", tmp_path), BANK_ONLY,
                          step_timeout_ms=300, poll_ms=50)
    result = engine.run(cap, {})
    assert result.status is Status.FAILED
    assert result.failure.kind is FailureKind.POLICY_BLOCKED
    assert surface.actions == [], "the blocked action must not have been performed"


def test_a_blocked_step_is_recorded_as_a_guardrail_working(tmp_path):
    """Not a malfunction. The log should show the system refusing, not breaking."""
    recorder = Recorder("probe", tmp_path)
    cap = capability(steps=[Step(index=0, intent="leave the allowlist",
                                 action=Navigate(url="https://evil.example.com/"))])
    ReplayEngine(ScriptedSurface(screen("Done")), recorder, BANK_ONLY,
                 step_timeout_ms=300, poll_ms=50).run(cap, {})
    assert any(event["kind"] == "policy_blocked" for event in recorder.events())


# -- what gets written down ------------------------------------------------

def test_regulated_data_shapes_are_scrubbed_from_captured_text():
    """The layer that catches what structure cannot: data the automation never
    handled as a value, but that was on screen when a snapshot was taken."""
    captured = (
        "Name Dana Whitfield  Date of Birth 1979-04-12\n"
        "SSN ***-**-4417  E-mail d.whitfield@example.org  Telephone (206) 555-0147"
    )
    scrubbed = Redactor().scrub(captured)
    assert "1979-04-12" not in scrubbed
    assert "***-**-4417" not in scrubbed
    assert "d.whitfield@example.org" not in scrubbed
    assert "(206) 555-0147" not in scrubbed
    assert "<date_of_birth>" in scrubbed and "<email>" in scrubbed


def test_a_full_ssn_is_scrubbed():
    assert "123-45-6789" not in Redactor().scrub("SSN 123-45-6789 on file")


def test_known_secrets_are_scrubbed_whole():
    """Replaced before the patterns run, so an exact secret is not half-eaten
    by a pattern on its way past."""
    assert Redactor(secrets={"Passw0rd!"}).scrub("used Passw0rd! to sign on") == \
        f"used {REDACTED} to sign on"


def test_a_secret_learned_mid_run_applies_to_everything_after_it():
    redactor = Redactor()
    assert "Dana Whitfield" in redactor.scrub("member Dana Whitfield")
    redactor.add_secret("Dana Whitfield")
    assert "Dana Whitfield" not in redactor.scrub("member Dana Whitfield")


def test_very_short_secrets_are_ignored():
    """Scrubbing every occurrence of a two-character value would corrupt a log
    into uselessness, and a secret that short is a different problem."""
    assert Redactor(secrets={"ab"}).scrub("a table of absolute values") == "a table of absolute values"


def test_redaction_reaches_into_nested_event_payloads():
    scrubbed = Redactor(secrets={"Passw0rd!"}).scrub_value(
        {"attempts": [{"note": "tried Passw0rd!"}], "count": 3})
    assert scrubbed["attempts"][0]["note"] == f"tried {REDACTED}"
    assert scrubbed["count"] == 3


def test_the_recorder_applies_pattern_redaction_to_snapshots(tmp_path):
    class Screen:
        def snapshot(self):
            return "Date of Birth 1979-04-12 / SSN ***-**-4417"

    recorder = Recorder("probe", tmp_path)
    recorder.snapshot(Screen(), "failure")
    written = (tmp_path / "probe" / "failure.txt").read_text()
    assert "1979-04-12" not in written and "***-**-4417" not in written
