"""Turning a discovery run into a capability.

Built from fabricated runs rather than by calling a model: the interesting
claims are about what synthesis derives from a given run, and those should be
tested without spending money or waiting on a network.

The rule that matters most here is the least obvious one. A discovery run sees
one member, so anything it picks up from the screen may describe that member
rather than the shape of the screen. Such a capability passes its own test and
fails for every other caller, which is the worst way for a defect to behave.
"""

import pytest

from cua.agent.loop import DiscoveryRun, RecordedAction
from cua.agent.synthesize import InputSpec, SynthesisError, synthesise
from cua.agent.tools import render, tools_for
from cua.safety import Policy
from cua.surfaces.base import Control, FrameView, Observation, Table

MEMBER = InputSpec(name="member_id", value="100234", description="the member")
SECRET = InputSpec(name="operator_password", value="Passw0rd!", description="the password",
                   sensitive=True)


def control(role="textbox", name="", labels=(), ordinal=0, value=None, handle="h"):
    return Control(frame="mainFrame", role=role, name=name, value=value,
                   labels=tuple(labels), ordinal=ordinal, handle=handle)


def screen(text="Member Information Accounts Current Balance", controls=(), tables=()):
    return Observation(url="http://x/member/100234", title="t",
                       frames=(FrameView("mainFrame", "http://x/", text),),
                       controls=tuple(controls), tables=tuple(tables))


ACCOUNTS = Table(frame="mainFrame",
                 headers=("Account Number", "Type", "Status", "Current Balance"),
                 rows=(("SAV-100234-01", "Savings", "Open", "$4,182.55"),))
DETAILS = Table(frame="mainFrame", headers=(),
                rows=(("Name", "Dana Whitfield", "Date of Birth", "1979-04-12"),))


def a_run(actions, *, success_text=("Current Balance",), outputs=(), final=None) -> DiscoveryRun:
    return DiscoveryRun(
        goal="read a balance", outcome="completed", actions=list(actions),
        finish_payload={"success_text": list(success_text), "outputs": list(outputs)},
        final_observation=final or screen(tables=[ACCOUNTS, DETAILS]), steps=len(actions),
    )


def build(run, inputs=(MEMBER,), **overrides):
    kw = dict(capability_id="probe", name="probe", description="d", application="Meridian Core",
              entry_point="http://x/", inputs=list(inputs), model="claude-opus-5", run_id="r")
    kw.update(overrides)
    return synthesise(run, **kw)


# -- targeting is derived from what the control reported --------------------

def test_a_named_control_is_targeted_by_role_and_name():
    typed = control(name="Member ID")
    run = a_run([RecordedAction(kind="type", why="enter the member ID", control=typed,
                                value="100234", observation_before=screen(controls=[typed]))])
    [strategy] = build(run).steps[0].action.target.strategies
    assert strategy.kind == "role_name" and strategy.confidence == "high"
    assert "accessible name" in strategy.rationale


def test_an_unnamed_control_is_targeted_by_its_adjacent_text():
    """The sub-account form's real shape."""
    typed = control(labels=["Initial Deposit"])
    run = a_run([RecordedAction(kind="type", why="enter the deposit", control=typed,
                                value="150.00", observation_before=screen(controls=[typed]))])
    [strategy] = build(run).steps[0].action.target.strategies
    assert strategy.kind == "cell_adjacent" and strategy.label_text == "Initial Deposit"
    assert "no accessible name at all" in strategy.rationale


def test_a_control_with_nothing_to_identify_it_falls_back_to_position():
    anonymous = control()
    run = a_run([RecordedAction(kind="click", why="press it", control=anonymous,
                                observation_before=screen(controls=[anonymous]))])
    [strategy] = build(run).steps[0].action.target.strategies
    assert strategy.kind == "ordinal" and strategy.confidence == "low"
    assert "Position is what is left" in strategy.rationale


def test_a_strategy_that_would_have_matched_two_controls_is_not_recorded():
    """Not a weaker option -- a wrong one. Keeping it as a fallback would mean
    a replay eventually resolving to the wrong control."""
    first = control(role="link", name="Open", ordinal=0, handle="a")
    second = control(role="link", name="Open", ordinal=1, handle="b")
    run = a_run([RecordedAction(kind="click", why="open the record", control=first,
                                observation_before=screen(controls=[first, second]))])
    kinds = [s.kind for s in build(run).steps[0].action.target.strategies]
    assert "role_name" not in kinds, "an ambiguous strategy was recorded as though it were sound"


# -- nothing recorded may describe one particular invocation ---------------

def test_a_row_anchor_never_carries_a_supplied_value():
    """Anchoring on SAV-100234-01 works for the recorded member and nobody else."""
    run = a_run(
        [RecordedAction(kind="click", why="open", control=control(role="link", name="Open"),
                        observation_before=screen(controls=[control(role="link", name="Open")]))],
        outputs=[{"name": "balance", "description": "d", "value": "$4,182.55", "sensitive": False}],
    )
    [output] = build(run).outputs
    assert output.extract.row_contains == "Savings"
    assert "100234" not in output.extract.row_contains


def test_a_success_phrase_carrying_a_supplied_value_is_discarded():
    run = a_run([RecordedAction(kind="navigate", why="open", url="http://x/")],
                success_text=["Member Detail - 100234", "Current Balance"],
                final=screen(text="Member Detail - 100234  Current Balance",
                             tables=[ACCOUNTS, DETAILS]))
    capability = build(run)
    asserted = [a.text for a in capability.checkpoint.assertions]
    assert asserted == ["Current Balance"]
    assert "Member Detail - 100234" in capability.provenance.notes


def test_a_run_whose_only_success_phrase_was_invocation_specific_is_refused():
    """Better to refuse than to save a capability that can only ever confirm
    success for the member it was recorded against."""
    run = a_run([RecordedAction(kind="navigate", why="open", url="http://x/")],
                success_text=["Member Detail - 100234"],
                final=screen(text="Member Detail - 100234", tables=[ACCOUNTS]))
    with pytest.raises(SynthesisError, match="describe this particular invocation"):
        build(run)


def test_a_success_phrase_that_was_not_on_screen_is_refused():
    """A model recalling the screen slightly wrong is an ordinary event, and
    would otherwise produce a checkpoint that fails every replay."""
    run = a_run([RecordedAction(kind="navigate", why="open", url="http://x/")],
                success_text=["Transfer Complete"])
    with pytest.raises(SynthesisError, match="not on the final screen"):
        build(run)


# -- parameters and credentials -------------------------------------------

def test_a_typed_value_the_caller_supplies_becomes_a_parameter_reference():
    typed = control(name="Member ID")
    run = a_run([RecordedAction(kind="type", why="enter the member ID", control=typed,
                                value="100234", observation_before=screen(controls=[typed]))])
    bound = build(run).steps[0].action.value
    assert bound.kind == "param" and bound.param == "member_id"


def test_a_value_the_caller_does_not_supply_stays_a_literal():
    typed = control(name="Branch")
    run = a_run([RecordedAction(kind="type", why="enter the branch", control=typed,
                                value="Ballard", observation_before=screen(controls=[typed]))])
    bound = build(run).steps[0].action.value
    assert bound.kind == "literal" and bound.value == "Ballard"


def test_no_credential_reaches_the_saved_capability():
    typed = control(name="Password")
    run = a_run([RecordedAction(kind="type", why="enter the password", control=typed,
                                value="Passw0rd!", observation_before=screen(controls=[typed]))])
    capability = build(run, inputs=(MEMBER, SECRET))
    assert "Passw0rd!" not in capability.model_dump_json()
    assert capability.steps[0].action.value.param == "operator_password"


def test_a_sensitive_parameter_gets_no_example_value():
    typed = control(name="Password")
    run = a_run([RecordedAction(kind="type", why="enter the password", control=typed,
                                value="Passw0rd!", observation_before=screen(controls=[typed]))])
    [parameter] = [p for p in build(run, inputs=(MEMBER, SECRET)).parameters if p.sensitive]
    assert parameter.example is None


# -- what synthesis will not do -------------------------------------------

def test_a_run_that_did_not_succeed_produces_nothing():
    run = DiscoveryRun(goal="g", outcome="gave_up", give_up_reason="dead end")
    with pytest.raises(SynthesisError, match="ended 'gave_up'"):
        build(run)


def test_a_synthesised_capability_is_always_a_draft():
    """A run that succeeds sees only the path that succeeds. Business outcomes
    and recovery describe what happens otherwise and cannot be discovered
    from it, so a synthesised capability is never approved."""
    run = a_run([RecordedAction(kind="navigate", why="open", url="http://x/")])
    capability = build(run)
    assert capability.approval == "draft"
    assert capability.business_outcomes == [] and capability.recovery == []
    assert "cannot be observed on a run that went right" in capability.provenance.notes


def test_an_output_that_cannot_be_located_again_is_refused():
    run = a_run([RecordedAction(kind="navigate", why="open", url="http://x/")],
                outputs=[{"name": "x", "description": "d", "value": "nowhere on screen",
                          "sensitive": False}])
    with pytest.raises(SynthesisError, match="could not be located"):
        build(run)


# -- the model's view of the world ----------------------------------------

def test_the_tool_surface_offers_only_what_the_policy_permits():
    """An action the model is never offered is one it cannot argue with,
    cannot work around, and does not waste a turn on."""
    narrow = Policy(allowed_origins=["http://x"], allowed_actions=["navigate", "click"])
    assert [t["name"] for t in tools_for(narrow)] == ["navigate", "click", "finish", "give_up"]


def test_the_model_sees_controls_numbered_with_their_nearby_text():
    rendered = render(screen(controls=[control(labels=["Initial Deposit"]),
                                       control(name="Submit Request", role="button", ordinal=0)]))
    assert "[0] textbox" in rendered and "nearby-text=['Initial Deposit']" in rendered
    assert "[1] button    name='Submit Request'" in rendered


def test_the_model_is_never_shown_markup():
    """It chooses from what the replay engine can resolve, so it cannot pick
    something the system would be unable to find again."""
    rendered = render(screen(controls=[control(name="Member ID")], tables=[ACCOUNTS]))
    assert "<" not in rendered and "css" not in rendered.lower()
