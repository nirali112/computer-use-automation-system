"""Target resolution, condition evaluation and extraction.

All of it runs against hand-built observations. That is the payoff of making
`Observation` a value rather than a live handle: the logic that decides which
control a recorded target means -- the logic most likely to silently operate
the wrong record -- is tested exhaustively, deterministically, and in
milliseconds, with no browser and no timing.
"""

import pytest

from cua.artifact import (
    AdjacentCell,
    CellAdjacent,
    Condition,
    ControlPresent,
    ControlText,
    Ordinal,
    RoleName,
    TableCell,
    Target,
    TextAbsent,
    TextPresent,
)
from cua.resolve import (
    ExtractionError,
    cast_value,
    evaluate_condition,
    extract_value,
    resolve_target,
)
from cua.surfaces.base import Control, FrameView, Observation, Table


def control(role="textbox", name="", labels=(), ordinal=0, frame="mainFrame", value=None):
    return Control(frame=frame, role=role, name=name, value=value, labels=tuple(labels),
                   ordinal=ordinal, handle=f"{role}-{ordinal}")


def observation(controls=(), tables=(), text="", frame="mainFrame"):
    return Observation(
        url="http://x/", title="t",
        frames=(FrameView(name=frame, url="http://x/", text=text),),
        controls=tuple(controls), tables=tuple(tables),
    )


def strategy_role_name(**kw):
    return RoleName(confidence="high", rationale="r", **kw)


def strategy_cell(**kw):
    return CellAdjacent(confidence="medium", rationale="r", **kw)


# -- resolution ------------------------------------------------------------

def test_resolves_on_role_and_accessible_name():
    obs = observation([control(role="textbox", name="Member ID")])
    target = Target(description="member id", strategies=[strategy_role_name(role="textbox", name="Member ID")])
    r = resolve_target(obs, target)
    assert r.ok and r.strategy == "role_name" and r.confidence == "high"


def test_falls_back_when_the_control_has_no_accessible_name():
    """The sub-account form's real behaviour: two inputs, neither named,
    distinguishable only by the cell beside them."""
    obs = observation([
        control(ordinal=0, labels=["Initial Deposit"]),
        control(ordinal=1, labels=["Account Nickname"]),
    ])
    target = Target(description="deposit", strategies=[
        strategy_role_name(role="textbox", name="Initial Deposit"),
        strategy_cell(role="textbox", label_text="Initial Deposit"),
    ])
    r = resolve_target(obs, target)
    assert r.ok and r.strategy == "cell_adjacent"
    assert r.control.ordinal == 0
    assert [(a.kind, a.matched, a.used) for a in r.attempts] == [
        ("role_name", 0, False), ("cell_adjacent", 1, True)]


def test_an_ambiguous_strategy_is_refused_rather_than_guessed():
    """Picking the first of several matches is how automation opens the wrong
    member's record. Failing is the correct behaviour here."""
    obs = observation([control(role="link", name="Open", ordinal=0),
                       control(role="link", name="Open", ordinal=1)])
    target = Target(description="open link", strategies=[strategy_role_name(role="link", name="Open")])
    r = resolve_target(obs, target)
    assert not r.ok
    assert r.attempts[0].matched == 2 and "refusing to guess" in r.attempts[0].note


def test_ambiguity_does_not_stop_a_later_strategy_from_resolving():
    obs = observation([control(role="link", name="Open", ordinal=0, labels=["Savings"]),
                       control(role="link", name="Open", ordinal=1, labels=["Checking"])])
    target = Target(description="the savings row's Open link", strategies=[
        strategy_role_name(role="link", name="Open"),
        strategy_cell(role="link", label_text="Savings"),
    ])
    r = resolve_target(obs, target)
    assert r.ok and r.control.ordinal == 0


def test_ordinal_is_available_as_an_explicit_last_resort():
    obs = observation([control(ordinal=0), control(ordinal=1)])
    target = Target(description="second box", strategies=[
        Ordinal(role="textbox", index=1, confidence="low", rationale="nothing else distinguishes them")])
    r = resolve_target(obs, target)
    assert r.ok and r.confidence == "low" and r.control.ordinal == 1


def test_resolution_is_scoped_to_the_declared_frame():
    """A frameset means the same accessible name can exist twice on screen."""
    obs = Observation(
        url="http://x/", title="t",
        frames=(FrameView("navFrame", "u", ""), FrameView("mainFrame", "u", "")),
        controls=(control(role="link", name="Member Search", frame="navFrame"),),
    )
    target = Target(description="search link", frame="mainFrame",
                    strategies=[strategy_role_name(role="link", name="Member Search")])
    assert not resolve_target(obs, target).ok
    target_nav = target.model_copy(update={"frame": "navFrame"})
    assert resolve_target(obs, target_nav).ok


def test_failure_names_every_strategy_and_why_it_did_not_work():
    obs = observation([control(name="Something Else")])
    target = Target(description="the deposit field", strategies=[
        strategy_role_name(role="textbox", name="Initial Deposit"),
        strategy_cell(role="textbox", label_text="Initial Deposit"),
    ])
    message = resolve_target(obs, target).describe_failure(target)
    assert "the deposit field" in message
    assert "role_name" in message and "cell_adjacent" in message


# -- conditions ------------------------------------------------------------

def test_text_present_and_absent():
    obs = observation(text="Member Detail - 100234\nAccounts")
    assert evaluate_condition(obs, Condition(description="d", assertions=[TextPresent(text="Member Detail")])).holds
    assert evaluate_condition(obs, Condition(description="d", assertions=[TextAbsent(text="No record found")])).holds
    assert not evaluate_condition(obs, Condition(description="d", assertions=[TextAbsent(text="Accounts")])).holds


def test_every_failed_assertion_is_reported_not_just_the_first():
    """One failure log should explain the whole gap between expected and actual."""
    obs = observation(text="Sign On")
    condition = Condition(description="d", assertions=[
        TextPresent(text="Member Detail"), TextPresent(text="Current Balance")])
    result = evaluate_condition(obs, condition)
    assert not result.holds and len(result.failed) == 2


def test_control_present_assertion_uses_the_same_resolution_rules():
    obs = observation([control(role="button", name="Continue")])
    target = Target(description="continue", strategies=[strategy_role_name(role="button", name="Continue")])
    assert evaluate_condition(obs, Condition(description="d", assertions=[ControlPresent(target=target)])).holds


# -- extraction ------------------------------------------------------------

def _accounts_grid():
    return Table(frame="mainFrame",
                 headers=("Account Number", "Type", "Status", "Current Balance"),
                 rows=(("SAV-100234-01", "Savings", "Open", "$4,182.55"),
                       ("CHK-100234-01", "Checking", "Open", "$913.20")))


def test_table_cell_is_located_by_row_content_and_column_header():
    obs = observation(tables=[_accounts_grid()])
    raw = extract_value(obs, TableCell(row_contains="Savings", column_header="Current Balance"),
                        pattern=r"\$([\d,]+\.\d{2})")
    assert raw == "4,182.55"
    assert cast_value(raw, "number") == 4182.55


def test_table_cell_picks_the_right_row_not_the_first():
    obs = observation(tables=[_accounts_grid()])
    assert extract_value(obs, TableCell(row_contains="Checking", column_header="Current Balance")) == "$913.20"


def test_adjacent_cell_reads_a_label_value_panel():
    """Detail panels have no column headers -- they are label/value pairs."""
    panel = Table(frame="mainFrame", headers=(),
                  rows=(("Name", "Dana Whitfield", "Date of Birth", "1979-04-12"),))
    assert extract_value(observation(tables=[panel]), AdjacentCell(label_text="Name")) == "Dana Whitfield"


def test_control_text_reads_a_resolved_control():
    obs = observation([control(role="textbox", name="Member ID", value="100234")])
    target = Target(description="member id", strategies=[strategy_role_name(role="textbox", name="Member ID")])
    assert extract_value(obs, ControlText(target=target)) == "100234"


def test_a_missing_extraction_target_says_precisely_what_was_missing():
    obs = observation(tables=[_accounts_grid()])
    with pytest.raises(ExtractionError, match="Vacation Club"):
        extract_value(obs, TableCell(row_contains="Vacation Club", column_header="Current Balance"))


def test_extracted_text_must_match_the_declared_pattern():
    """A silently wrong number is worse than a loud failure."""
    panel = Table(frame="mainFrame", headers=(), rows=(("Balance", "unavailable"),))
    with pytest.raises(ExtractionError, match="does not match the declared pattern"):
        extract_value(observation(tables=[panel]), AdjacentCell(label_text="Balance"),
                      pattern=r"\$([\d,]+\.\d{2})")


def test_money_formatting_is_stripped_when_casting_to_a_number():
    assert cast_value("$1,234.50", "number") == 1234.50
    assert cast_value("42", "integer") == 42
    assert cast_value("$4,182.55", "string") == "$4,182.55"
