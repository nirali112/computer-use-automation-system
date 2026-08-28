"""The web surface, against the real application in a real browser.

Everything else is tested against constructed observations. This is the one
place that checks the surface actually reports what the application shows --
because the interesting claims are empirical: that accessibility names
survive a frameset, that an unlabelled input really does come back with an
empty name, and that acting through an accessibility node handle really does
put text in the right field.
"""

import pytest

from cua.artifact import AdjacentCell, CellAdjacent, RoleName, TableCell, Target
from cua.resolve import cast_value, extract_value, resolve_target
from cua.surfaces.web import WebSurface


@pytest.fixture(scope="module")
def surface():
    s = WebSurface()
    yield s
    s.close()


def _target(*strategies, description="a control", frame="mainFrame"):
    return Target(description=description, frame=frame, strategies=list(strategies))


def _by_name(role, name, confidence="high"):
    return RoleName(role=role, name=name, confidence=confidence, rationale="r")


def _by_cell(role, label):
    return CellAdjacent(role=role, label_text=label, confidence="medium", rationale="r")


def _act(surface, target, text=None):
    resolution = resolve_target(surface.observe(), target)
    assert resolution.ok, resolution.describe_failure(target)
    if text is None:
        surface.invoke(resolution.control)
    else:
        surface.enter_text(resolution.control, text)
    return resolution


@pytest.fixture(scope="module")
def signed_on(surface, base_url):
    surface.navigate(f"{base_url}/")
    _act(surface, _target(_by_name("textbox", "Operator ID")), "teller01")
    _act(surface, _target(_by_name("textbox", "Password")), "Passw0rd!")
    _act(surface, _target(_by_name("button", "Sign On")))
    return surface


def test_a_frameset_is_observed_frame_by_frame(signed_on):
    """A page-level accessibility tree would be empty here: the root document
    contains only the frames."""
    observation = signed_on.observe()
    assert {f.name for f in observation.frames} == {"(root)", "navFrame", "mainFrame"}
    assert observation.controls_in("mainFrame")
    assert observation.controls_in("navFrame")


def test_identical_names_in_different_frames_do_not_collide(signed_on):
    """Both frames offer something called Member Search."""
    observation = signed_on.observe()
    nav = _target(_by_name("link", "Member Search"), frame="navFrame")
    assert resolve_target(observation, nav).ok
    assert not resolve_target(observation, nav.model_copy(update={"frame": "mainFrame"})).ok


def test_a_labelled_field_resolves_by_its_accessible_name(signed_on):
    resolution = _act(signed_on, _target(_by_name("textbox", "Member ID")), "100234")
    assert resolution.strategy == "role_name"
    _act(signed_on, _target(_by_name("button", "Search")))
    _act(signed_on, _target(_by_name("link", "Open", confidence="medium")))
    assert "Member Detail" in signed_on.observe().text_in("mainFrame")


def test_outputs_are_extracted_and_typed_from_the_live_screen(signed_on):
    observation = signed_on.observe()
    name = extract_value(observation, AdjacentCell(label_text="Name"))
    balance = extract_value(observation, TableCell(row_contains="Savings", column_header="Current Balance"),
                            pattern=r"\$([\d,]+\.\d{2})")
    assert name == "Dana Whitfield"
    assert cast_value(balance, "number") == 4182.55


def test_the_unlabelled_form_reports_anonymous_controls(signed_on):
    """The empirical fact the whole targeting design follows from."""
    _act(signed_on, _target(_by_name("link", "Open Sub-Account")))
    boxes = [c for c in signed_on.observe().controls_in("mainFrame") if c.role == "textbox"]
    assert len(boxes) == 2
    assert all(box.name == "" for box in boxes), "expected the form to supply no accessible names"
    assert {label for box in boxes for label in box.labels} >= {"Initial Deposit", "Account Nickname"}


def test_anonymous_controls_are_still_targeted_correctly(signed_on):
    """Resolution by adjacent cell, then proof the text landed in the right box."""
    deposit = _target(_by_name("textbox", "Initial Deposit"), _by_cell("textbox", "Initial Deposit"),
                      description="the Initial Deposit field")
    nickname = _target(_by_name("textbox", "Account Nickname"), _by_cell("textbox", "Account Nickname"),
                       description="the Account Nickname field")

    assert _act(signed_on, deposit, "250.00").strategy == "cell_adjacent"
    assert _act(signed_on, nickname, "Holiday Fund").strategy == "cell_adjacent"

    values = {label: box.value
              for box in signed_on.observe().controls_in("mainFrame") if box.role == "textbox"
              for label in box.labels[:1]}
    assert values["Initial Deposit"] == "250.00"
    assert values["Account Nickname"] == "Holiday Fund"


def test_a_snapshot_records_what_the_automation_could_see(signed_on):
    """The richer failure signal: a screenshot cannot show that a field
    reported an empty accessible name, and that is what breaks targeting."""
    text = signed_on.snapshot()
    assert "[frame mainFrame]" in text
    assert "labels=['Initial Deposit'" in text
