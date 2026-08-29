"""The artifact contract.

The capability artifact is the thing an agent invokes and a reviewer signs
off, so the properties worth testing are the ones that would let a wrong or
misunderstood artifact reach a live banking system: silent corruption on a
round trip, a schema version drifting unnoticed, and an artifact whose parts
contradict each other.
"""

import json

import pytest
from pydantic import ValidationError

from cua.artifact import (
    Capability,
    Checkpoint,
    Navigate,
    Parameter,
    ParamValue,
    Provenance,
    RoleName,
    SchemaVersionError,
    Step,
    Surface,
    Target,
    TextPresent,
    TypeText,
    catalog,
    load,
    load_latest,
    save,
)
from cua.artifact.capability import SCHEMA_VERSION
from reference_capability import member_balance_capability


# -- round trip ------------------------------------------------------------

def test_artifact_survives_a_round_trip_unchanged(tmp_path):
    original = member_balance_capability()
    reloaded = load(save(original, tmp_path))
    assert reloaded.model_dump() == original.model_dump()


def test_discriminated_unions_reload_as_their_own_types(tmp_path):
    """A step list that reloads as generic dictionaries would be useless.

    Actions, targeting strategies, extractions and remedies are all unions.
    Losing the concrete type on load is the kind of failure that shows up as
    a mysterious AttributeError mid-replay, so it is asserted directly.
    """
    reloaded = load(save(member_balance_capability(), tmp_path))
    assert reloaded.steps[0].action.kind == "navigate"
    assert reloaded.steps[1].action.value.kind == "param"
    assert [s.kind for s in reloaded.steps[1].action.target.strategies] == ["role_name", "cell_adjacent"]
    assert reloaded.outputs[1].extract.kind == "table_cell"
    assert reloaded.recovery[1].remedy.kind == "reauthenticate"


def test_stored_artifact_is_readable_json(tmp_path):
    """Reviewability is a requirement, not a nicety: a reviewer reads this."""
    text = save(member_balance_capability(), tmp_path).read_text()
    assert json.loads(text)["id"] == "member_savings_balance"
    assert text.count("\n") > 50  # indented, not minified onto one line


# -- versioning ------------------------------------------------------------

def test_unknown_schema_version_is_refused_not_coerced(tmp_path):
    path = save(member_balance_capability(), tmp_path)
    raw = json.loads(path.read_text())
    raw["schema_version"] = "0.9"
    path.write_text(json.dumps(raw))
    with pytest.raises(SchemaVersionError, match="0.9"):
        load(path)


def test_versions_are_kept_side_by_side_and_latest_wins(tmp_path):
    first = member_balance_capability()
    second = member_balance_capability()
    second.version = 2
    second.description = "revised"
    save(first, tmp_path)
    save(second, tmp_path)
    assert load_latest("member_savings_balance", tmp_path).version == 2
    assert load_latest("member_savings_balance", tmp_path).description == "revised"


def test_catalog_lists_each_capability_once_at_its_latest_version(tmp_path):
    c = member_balance_capability()
    save(c, tmp_path)
    c.version = 3
    save(c, tmp_path)
    listed = catalog(tmp_path)
    assert len(listed) == 1 and listed[0].version == 3


# -- internal consistency --------------------------------------------------

def _minimal(**overrides) -> dict:
    base = dict(
        id="probe",
        name="probe",
        description="d",
        surface=Surface(application="Meridian Core", entry_point="http://x/"),
        steps=[Step(index=0, intent="open", action=Navigate(url="http://x/"))],
        checkpoint=Checkpoint(description="c", assertions=[TextPresent(text="x")]),
        provenance=Provenance(recorded_by="test", run_id="r"),
    )
    base.update(overrides)
    return base


def test_step_binding_an_undeclared_parameter_is_rejected():
    """Caught at load time rather than at step seven of a live replay."""
    target = Target(
        description="a field",
        strategies=[RoleName(role="textbox", name="X", confidence="high", rationale="r")],
    )
    with pytest.raises(ValidationError, match="undeclared parameter 'nope'"):
        Capability(**_minimal(
            parameters=[Parameter(name="declared", type="string", description="d")],
            steps=[Step(index=0, intent="type", action=TypeText(target=target, value=ParamValue(param="nope")))],
        ))


def test_steps_must_be_indexed_consecutively():
    with pytest.raises(ValidationError, match="consecutively"):
        Capability(**_minimal(steps=[
            Step(index=0, intent="a", action=Navigate(url="http://x/")),
            Step(index=7, intent="b", action=Navigate(url="http://y/")),
        ]))


def test_duplicate_business_outcome_codes_are_rejected():
    """Codes are the caller's branch keys, so ambiguity is a contract break."""
    from cua.artifact import BusinessOutcome, Condition

    dup = [
        BusinessOutcome(code="SAME", description="a",
                        detect=Condition(description="d", assertions=[TextPresent(text="a")])),
        BusinessOutcome(code="SAME", description="b",
                        detect=Condition(description="d", assertions=[TextPresent(text="b")])),
    ]
    with pytest.raises(ValidationError, match="outcome codes must be unique"):
        Capability(**_minimal(business_outcomes=dup))


def test_a_capability_must_have_at_least_one_step():
    with pytest.raises(ValidationError):
        Capability(**_minimal(steps=[]))


def test_every_target_carries_at_least_one_strategy():
    with pytest.raises(ValidationError):
        Target(description="nothing identifies this", strategies=[])


# -- the agent-facing contract ---------------------------------------------

def test_input_schema_is_the_callable_contract():
    schema = member_balance_capability().input_schema()
    assert schema["required"] == ["member_id", "operator_id", "operator_password"]
    assert schema["properties"]["member_id"]["pattern"] == r"^\d{6}$"
    assert schema["additionalProperties"] is False


def test_tool_definition_advertises_the_business_outcomes():
    """A calling agent has to know in advance that 'no such member' is an
    answer it may receive, or it will treat one as a failure."""
    tool = member_balance_capability().as_tool_definition()
    assert tool["name"] == "member_savings_balance"
    assert "MEMBER_NOT_FOUND" in tool["description"]
    assert "SIGNON_FAILED" in tool["description"]


def test_sensitive_parameters_are_declared_for_redaction():
    assert member_balance_capability().sensitive_parameters() == {"operator_id", "operator_password"}


def test_no_sensitive_value_is_ever_stored_in_an_artifact(tmp_path):
    """The artifact holds parameter *references*, never values, so there is
    nothing in the stored file for redaction to have to strip."""
    text = save(member_balance_capability(), tmp_path).read_text()
    assert "Passw0rd!" not in text and "teller01" not in text
    assert '"kind": "param"' in text


def test_schema_version_is_stamped_on_every_artifact():
    assert member_balance_capability().schema_version == SCHEMA_VERSION


def test_a_sensitive_parameter_may_not_carry_an_example():
    """An example is documentation, and documentation gets committed. A
    sample credential in the `example` field is a real value in a reviewed,
    diffed, shared file -- so the schema refuses it outright."""
    with pytest.raises(ValidationError, match="must not carry an example"):
        Parameter(name="password", type="string", description="d",
                  sensitive=True, example="Passw0rd!")
