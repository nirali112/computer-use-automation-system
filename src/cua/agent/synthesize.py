"""Turning a successful run into a capability others can invoke.

The model discovered a path. This turns that path into a contract, and the
division of labour is deliberate: the model contributes intent, the system
contributes durability.

Asking a model to write a locator asks it to be good at what it is worst at --
predicting which attribute survives the next release. So it never does. It
picks a control, and this derives the targeting from what that control
actually reported: its accessible name if it had one, the text beside it if it
did not, its position only if there was nothing else.

Every derived strategy is then checked against the screen it was recorded on.
A strategy that would have matched two controls is demoted rather than
recorded as though it were sound, so the confidence written into the artifact
is a measurement, not an opinion. That check is also the only reason the
rationales are worth reading.

Outputs work the same way. The model quotes the value it read; this finds
where that value lives -- a grid cell located by row and column, a value
beside its label -- and records the extraction. The model says what it found,
the system works out how to find it again.

One thing this cannot do, and says so rather than pretending: a run that
succeeds sees only the path that succeeds. Business outcomes, recovery rules
and failure signals describe what happens when things go otherwise, and they
cannot be discovered from a happy path. So a synthesised capability is always
a draft, and the approval gate is what keeps a draft from acting unattended.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..artifact.capability import (
    AdjacentCell,
    Capability,
    ControlText,
    Output,
    Parameter,
    Provenance,
    Surface as SurfaceSpec,
    TableCell,
)
from ..artifact.conditions import Checkpoint, TextPresent
from ..artifact.steps import (
    Action,
    Click,
    LiteralValue,
    Navigate,
    ParamValue,
    SelectOption,
    Step,
    TypeText,
)
from ..artifact.targeting import CellAdjacent, Ordinal, RoleName, Strategy, Target
from ..resolve import resolve_target
from ..surfaces.base import Control, Observation
from .loop import DiscoveryRun, RecordedAction

MONEY = re.compile(r"^\$[\d,]+\.\d{2}$")


class SynthesisError(Exception):
    """The run cannot be turned into a capability that would be safe to keep."""


@dataclass
class InputSpec:
    """An argument the caller will supply on every invocation."""

    name: str
    value: str
    description: str
    sensitive: bool = False
    pattern: str | None = None


def _strategies_for(control: Control, observation: Observation | None) -> list[Strategy]:
    """Derive targeting from what the control reported, strongest first."""
    # Annotated, because inference from the first append would type this as a
    # list of RoleName and quietly exclude the adjacent-cell fallback -- which
    # is the strategy the whole design depends on being able to add.
    candidates: list[Strategy] = []

    if control.name:
        candidates.append(RoleName(
            role=control.role, name=control.name, confidence="high",
            rationale=(
                f"The browser computed the accessible name {control.name!r} for this control, "
                f"resolving whatever label, title or inner text supplies it. Role and name is "
                f"the most durable identification available, and is the same primitive the "
                f"platform accessibility APIs expose on native applications."
            ),
        ))

    for label in control.labels:
        if label == control.name or "\n" in label:
            continue  # the control's own name, or a whole option list, is not a label
        candidates.append(CellAdjacent(
            role=control.role, label_text=label, confidence="medium",
            rationale=(
                f"This control is identified by the adjacent text {label!r} in the surrounding "
                f"layout, which is how an operator reads the form. Used because the control "
                f"{'has no accessible name at all' if not control.name else 'may lose its name'}; "
                f"it survives restyling and renaming of the underlying control, and breaks only "
                f"if the form is restructured."
            ),
        ))

    def positional() -> Ordinal:
        return Ordinal(
            role=control.role, index=control.ordinal, confidence="low",
            rationale=(
                f"Nothing else identifies this control uniquely on the screen it was recorded "
                f"from. Position is what is left, and it is recorded at low confidence because "
                f"inserting any {control.role} ahead of it silently changes what this points at."
            ),
        )

    if observation is None:
        return candidates or [positional()]

    # Keep only strategies that would actually have worked on the screen they
    # were recorded from -- and that resolved to *this* control. A strategy
    # matching two controls is not a weaker option, it is a wrong one, and
    # keeping it as a fallback would mean a replay eventually resolving to the
    # wrong record. Falling back to position is the honest alternative:
    # low confidence, and visible as such in the artifact and in every log.
    def verifies(strategy) -> bool:
        probe = Target(description="probe", frame=control.frame, strategies=[strategy])
        resolution = resolve_target(observation, probe)
        return resolution.control is not None and resolution.control.handle == control.handle

    kept = [strategy for strategy in candidates if verifies(strategy)]
    if kept:
        return kept
    fallback = positional()
    return [fallback] if verifies(fallback) else []


def _target_for(action: RecordedAction) -> Target:
    control = action.control
    if control is None:
        raise SynthesisError(f"the recorded action {action.why!r} has no control to target")
    strategies = _strategies_for(control, action.observation_before)
    if not strategies:
        raise SynthesisError(
            f"no way to identify the {control.role} used for {action.why!r} was found; "
            f"it reported no name, no adjacent text and no stable position"
        )
    described = control.name or (control.labels[0] if control.labels else f"{control.role}")
    return Target(description=f"the {described!r} {control.role}", frame=control.frame,
                  strategies=strategies)


def _value_for(action: RecordedAction, inputs: list[InputSpec]):
    """Bind a typed value to a parameter when it is one, otherwise keep it.

    A value the caller supplied becomes a reference, which is what makes the
    recording reusable and is also why no credential is ever stored: the
    artifact holds the parameter's name, never the operator's password.
    """
    for spec in inputs:
        if action.value == spec.value:
            return ParamValue(param=spec.name)
    return LiteralValue(value=action.value or "")


def _varies(text: str, inputs: list[InputSpec]) -> bool:
    """Whether a piece of recorded text contains something that changes per call.

    The single most important check in synthesis, and the least obvious. A
    discovery run sees one member, so anything it picks up from the screen may
    be that member rather than the shape of the screen -- a row anchored on
    "SAV-100234-01", a checkpoint asserting "Member Detail - 100234". Both
    work perfectly for the member that was recorded and fail for every other
    one, which is the worst kind of defect: it passes its own test.

    So anything derived from a run is rejected if it contains a value the
    caller supplies. What survives describes the screen; what is dropped
    described one visit to it.
    """
    return any(spec.value and spec.value in text for spec in inputs)


def _extraction_for(name: str, value: str, observation: Observation, inputs: list[InputSpec]):
    """Work out where a value the model quoted actually lives."""
    for table in observation.tables:
        if table.headers:
            for row in table.rows:
                if value not in row:
                    continue
                column = table.headers[row.index(value)]
                # The anchor has to identify the row for any caller, not just
                # this one, so a cell carrying a supplied value is no anchor.
                anchor = next((cell for cell in row
                               if cell and cell != value and not _varies(cell, inputs)), None)
                if anchor:
                    return TableCell(frame=table.frame, row_contains=anchor, column_header=column)
        for row in table.rows:
            for index, cell in enumerate(row):
                label = row[index - 1].strip() if index > 0 else ""
                if cell == value and label and not _varies(label, inputs):
                    return AdjacentCell(frame=table.frame, label_text=label)

    for control in observation.controls:
        if control.value == value:
            probe = Target(description=name, frame=control.frame,
                           strategies=_strategies_for(control, observation))
            return ControlText(target=probe)

    raise SynthesisError(
        f"the run reported an output {name!r} with value {value!r}, but that value could not be "
        f"located on the final screen, so there is no way to read it again"
    )


def synthesise(
    run: DiscoveryRun,
    *,
    capability_id: str,
    name: str,
    description: str,
    application: str,
    entry_point: str,
    inputs: list[InputSpec],
    model: str,
    run_id: str,
) -> Capability:
    if not run.succeeded:
        raise SynthesisError(f"the discovery run ended {run.outcome!r}; there is nothing to record")
    if not run.final_observation:
        raise SynthesisError("the run recorded no final state")

    payload = run.finish_payload or {}
    final = run.final_observation

    steps: list[Step] = []
    for index, action in enumerate(run.actions):
        # Annotated for the same reason as the strategy list: inference from the
        # first branch would type this as Navigate and reject every other action.
        built: Action
        if action.kind == "navigate":
            built = Navigate(url=action.url or "")
        elif action.kind == "click":
            built = Click(target=_target_for(action))
        elif action.kind == "type":
            built = TypeText(target=_target_for(action), value=_value_for(action, inputs))
        elif action.kind == "select":
            built = SelectOption(target=_target_for(action), value=_value_for(action, inputs))
        else:
            raise SynthesisError(f"unrecorded action kind {action.kind!r}")
        steps.append(Step(index=index, intent=action.why, action=built))

    # Only success phrases that are genuinely on the final screen become
    # assertions. A checkpoint asserting something that was never there would
    # fail every replay, and a model recalling the screen slightly wrong is a
    # thoroughly ordinary way for that to happen.
    claimed = [phrase for phrase in payload.get("success_text", []) if phrase]
    on_screen = [p for p in claimed if any(p in frame.text for frame in final.frames)]
    verified = [p for p in on_screen if not _varies(p, inputs)]
    dropped = [p for p in on_screen if _varies(p, inputs)]
    if not verified:
        raise SynthesisError(
            f"no usable success phrase survived from {claimed}: "
            f"{[p for p in claimed if p not in on_screen]} were not on the final screen and "
            f"{dropped} describe this particular invocation rather than the screen"
        )

    outputs: list[Output] = []
    for declared in payload.get("outputs", []):
        raw = declared["value"]
        extraction = _extraction_for(declared["name"], raw, final, inputs)
        outputs.append(Output(
            name=declared["name"],
            type="number" if MONEY.match(raw) else "string",
            description=declared["description"],
            sensitive=bool(declared.get("sensitive")),
            extract=extraction,
            pattern=r"\$([\d,]+\.\d{2})" if MONEY.match(raw) else None,
        ))

    capability = Capability(
        id=capability_id,
        version=1,
        name=name,
        description=description,
        surface=SurfaceSpec(kind="web", application=application, entry_point=entry_point),
        parameters=[Parameter(name=spec.name, type="string", description=spec.description,
                              sensitive=spec.sensitive, pattern=spec.pattern,
                              example=None if spec.sensitive else spec.value)
                    for spec in inputs],
        outputs=outputs,
        steps=steps,
        checkpoint=Checkpoint(
            description=f"the application shows that the goal was reached: {run.goal}",
            assertions=[TextPresent(text=phrase) for phrase in verified],
        ),
        provenance=Provenance(
            recorded_by=model,
            run_id=run_id,
            steps_taken=run.steps,
            notes=(
                "Synthesised from a single successful discovery run. It records the path that "
                "worked and nothing else: business outcomes, recovery rules and failure signals "
                "describe what happens when things go otherwise and cannot be observed on a run "
                "that went right. They are added at review, which is what the draft state is for."
                + (f" Success phrases {dropped} were discarded because they contain values the "
                   f"caller supplies, so they describe this invocation rather than the screen."
                   if dropped else "")
            ),
        ),
        approval="draft",
    )

    # Last line of defence. Every mechanism above should already prevent this;
    # a stored credential is severe enough to be worth checking anyway.
    stored = capability.model_dump_json()
    for spec in inputs:
        if spec.sensitive and spec.value in stored:
            raise SynthesisError(
                f"refusing to save this capability: the value of the sensitive parameter "
                f"{spec.name!r} appears in the artifact"
            )
    return capability
