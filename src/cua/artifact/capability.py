"""The capability artifact: what a discovery run turns into.

The brief asks for "a typed, versioned description of the flow ... decoupled
from the raw model transcript". The shaping decision here is to treat it as a
*contract* rather than a recording. A step list says what happened once. A
contract says what an agent may ask for, what it will get back, what
legitimate answers other than success exist, and how anyone can tell whether
the flow really worked. Only the contract is safe to invoke unattended.

So a capability declares five things beyond its steps:

    parameters          what the caller supplies, typed, with the sensitive
                        ones marked so their values are never stored
    outputs             what the caller receives, typed, with the extraction
                        that produces each one
    checkpoint          the condition proving the flow reached its goal
    business_outcomes   the legitimate non-success answers, each with its own
                        code and its own detector
    recovery            the known, transient conditions replay may resolve by
                        itself

That fourth item is the one that matters most. "No such member" is an answer
the caller needs, not a crash; conflating the two is the most common way this
kind of system is got wrong. Declaring outcomes in the artifact means the
distinction is a property of the capability -- reviewable, versioned, and the
same on every run -- rather than an accident of exception handling.

The artifact deliberately does not contain the model transcript. It carries a
reference to it, so the reasoning is recoverable for audit, while the thing
that gets executed stays small enough to read and review.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, model_validator

from .conditions import Checkpoint, Condition
from .steps import ParamValue, Step
from .targeting import Target

SCHEMA_VERSION = "1.0"

ValueType = Literal["string", "integer", "number", "boolean"]

_JSON_TYPES: dict[str, str] = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
}


# ---------------------------------------------------------------------------
# the contract: what goes in, what comes out
# ---------------------------------------------------------------------------

class Parameter(BaseModel):
    name: str
    type: ValueType
    description: str
    required: bool = True
    sensitive: bool = Field(
        default=False,
        description="Credentials and regulated personal data. A sensitive "
        "parameter's value is supplied per invocation and never written to "
        "an artifact, a log or an evidence file. Marking it here is what "
        "lets redaction be enforced generically instead of by pattern-"
        "matching hopefully at the logging layer.",
    )
    pattern: str | None = Field(
        default=None, description="Optional regular expression the value must match."
    )
    example: str | None = None

    @model_validator(mode="after")
    def _sensitive_parameters_carry_no_example(self) -> "Parameter":
        """A sensitive parameter may not carry an example value.

        Found by a test asserting that no artifact contains a credential.
        An example is documentation, and documentation gets committed: a
        sample operator ID or password sitting in the `example` field is a
        real value in a file that is reviewed, diffed and shared. Since the
        rule can be enforced structurally, it is -- rather than left as
        guidance nobody reads.
        """
        if self.sensitive and self.example is not None:
            raise ValueError(
                f"parameter {self.name!r} is marked sensitive and must not carry an "
                f"example; the example would be stored in the artifact"
            )
        return self

    def json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": _JSON_TYPES[self.type], "description": self.description}
        if self.pattern:
            schema["pattern"] = self.pattern
        if self.example is not None:
            schema["examples"] = [self.example]
        return schema


class ControlText(BaseModel):
    """Read the text of a control found by the usual targeting rules."""

    kind: Literal["control_text"] = "control_text"
    target: Target


class TableCell(BaseModel):
    """Read a cell from a grid by naming its row and its column.

    The balance on the member detail screen sits in an unremarkable table
    cell with nothing to identify it. What locates it is the same thing an
    operator uses: find the row that says "Savings", then read the column
    headed "Current Balance". Recording it that way survives added columns,
    reordered rows and restyling, and it fails loudly if the grid genuinely
    changes shape.
    """

    kind: Literal["table_cell"] = "table_cell"
    frame: str = "mainFrame"
    row_contains: str
    column_header: str


class AdjacentCell(BaseModel):
    """Read the value beside a label in a label/value table.

    Distinct from `TableCell`, and both are needed. Grids have column
    headers, so a cell is located by row and column. Detail panels do not:
    they are two-column tables where the left cell names the field and the
    right cell holds the value. Reading "the cell to the right of the one
    saying Name" is how an operator reads it, and it is stable against the
    panel being restyled or having fields added around it.
    """

    kind: Literal["adjacent_cell"] = "adjacent_cell"
    frame: str = "mainFrame"
    label_text: str
    direction: Literal["right", "below"] = "right"


Extraction = Annotated[Union[ControlText, TableCell, AdjacentCell], Field(discriminator="kind")]


class Output(BaseModel):
    name: str
    type: ValueType
    description: str
    extract: Extraction
    pattern: str | None = Field(
        default=None,
        description="Optional regular expression applied to the extracted "
        "text; the first capturing group becomes the value. Used to lift a "
        "number out of a formatted string such as '$4,182.55'.",
    )

    def json_schema(self) -> dict[str, Any]:
        return {"type": _JSON_TYPES[self.type], "description": self.description}


# ---------------------------------------------------------------------------
# the non-success answers
# ---------------------------------------------------------------------------

class BusinessOutcome(BaseModel):
    """A legitimate result that is not the goal being met.

    An unknown member, a rejected deposit, a permission denial. Replay that
    detects one of these has *succeeded*: it drove the application correctly
    and is reporting what the application said. The caller is told which
    outcome, by code, and can act on it.
    """

    code: str = Field(
        description="Stable, machine-readable identifier the caller branches "
        "on -- MEMBER_NOT_FOUND. Part of the capability's public contract, so "
        "changing one is a version-bumping change."
    )
    description: str
    detect: Condition
    after_step: int | None = Field(
        default=None,
        description="Restricts detection to after a given step, where an "
        "outcome would otherwise be ambiguous. Unset means it may be "
        "detected at any point.",
    )


class Dismiss(BaseModel):
    """Clear a known interstitial by operating its own control."""

    kind: Literal["dismiss"] = "dismiss"
    target: Target


class Retry(BaseModel):
    """Wait and repeat the current step, for genuinely transient conditions."""

    kind: Literal["retry"] = "retry"
    delay_ms: int = Field(default=2_000, ge=0)


class Reauthenticate(BaseModel):
    """Re-establish the session, then resume from the current step.

    Session expiry is the one interruption that is both common and safely
    recoverable, because signing back on restores the state the flow assumed
    rather than changing anything.
    """

    kind: Literal["reauthenticate"] = "reauthenticate"


Remedy = Annotated[Union[Dismiss, Retry, Reauthenticate], Field(discriminator="kind")]


class Recovery(BaseModel):
    """A condition replay is permitted to resolve without escalating.

    Bounded on purpose. Each rule states how many attempts it may make, and
    exhausting them is a failure rather than grounds for trying something
    else. Open-ended self-healing is how automation ends up doing something
    nobody sanctioned.
    """

    name: str
    description: str
    detect: Condition
    remedy: Remedy
    max_attempts: int = Field(default=2, ge=1, le=5)


# ---------------------------------------------------------------------------
# where it runs, and where it came from
# ---------------------------------------------------------------------------

class Surface(BaseModel):
    """What kind of surface the capability was recorded against.

    `kind` is the seam. Steps, targets and assertions are written in terms of
    roles, accessible names and structural relationships, none of which are
    web concepts, so the same artifact shape describes a desktop application
    driven through platform accessibility APIs. Only the executor changes.

    The application identity fields carry the multi-tenant story. Hundreds of
    institutions run the same vendor products, so a capability is recorded
    against a *product*, and a tenant is a variant of it -- see `variant_of`.
    """

    kind: Literal["web", "desktop"] = "web"
    application: str = Field(description="The vendor product, e.g. 'Meridian Core'.")
    application_version: str | None = None
    entry_point: str = Field(description="Where the flow starts: a URL, or a launch target.")
    tenant: str | None = Field(
        default=None,
        description="The institution this recording came from. Unset means "
        "the capability is the shared base for the product.",
    )
    variant_of: str | None = Field(
        default=None,
        description="The base capability this one specialises. A tenant that "
        "brands or configures the product differently overrides only the "
        "steps that actually differ, rather than re-recording the flow. The "
        "resolution of overrides is designed here, not implemented.",
    )


class Provenance(BaseModel):
    """How this artifact came to exist.

    Enough to audit and reproduce, deliberately not the transcript itself:
    the executable contract stays small and reviewable, while the reasoning
    behind it remains retrievable by reference.
    """

    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    recorded_by: str = Field(description="The model that drove the discovery run.")
    run_id: str = Field(description="Identifies the evidence directory for that run.")
    steps_taken: int | None = Field(
        default=None, description="Actions in the discovery run, before pruning to the flow."
    )
    notes: str | None = None


# ---------------------------------------------------------------------------
# the capability
# ---------------------------------------------------------------------------

class Capability(BaseModel):
    """A reusable, reviewable, agent-invocable flow."""

    schema_version: str = Field(
        default=SCHEMA_VERSION,
        description="Version of this schema. A loader refuses an artifact it "
        "does not understand rather than coercing it, because a silently "
        "misread capability would go on to operate a banking system.",
    )
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$", description="Stable slug; the invocation name.")
    version: int = Field(default=1, ge=1, description="Bumped whenever the contract or the flow changes.")
    name: str
    description: str = Field(description="What this does, written for the agent that will call it.")

    surface: Surface
    parameters: list[Parameter] = Field(default_factory=list)
    outputs: list[Output] = Field(default_factory=list)
    steps: list[Step] = Field(min_length=1)
    checkpoint: Checkpoint = Field(description="Proves the goal was reached. Verified on every replay.")
    business_outcomes: list[BusinessOutcome] = Field(default_factory=list)
    recovery: list[Recovery] = Field(default_factory=list)

    provenance: Provenance
    approval: Literal["draft", "approved"] = Field(
        default="draft",
        description="A freshly recorded capability is a draft. Gating "
        "unattended replay on review is the cheapest guard against a flow "
        "that happened to work once being trusted in production.",
    )

    # -- integrity ---------------------------------------------------------

    @model_validator(mode="after")
    def _check_internal_references(self) -> "Capability":
        """Reject an artifact whose parts disagree with each other.

        These are exactly the mistakes a generated artifact makes, and every
        one of them would otherwise surface as a confusing runtime failure
        halfway through operating a live application. Catching them at load
        time is much cheaper than catching them at step 7 of a replay.
        """
        declared = {p.name for p in self.parameters}
        for step in self.steps:
            value = getattr(step.action, "value", None)
            if isinstance(value, ParamValue) and value.param not in declared:
                raise ValueError(
                    f"step {step.index} binds undeclared parameter {value.param!r}; "
                    f"declared parameters are {sorted(declared)}"
                )

        if [s.index for s in self.steps] != list(range(len(self.steps))):
            raise ValueError("steps must be indexed consecutively from zero, in execution order")

        names = [o.name for o in self.outputs]
        if len(names) != len(set(names)):
            raise ValueError("output names must be unique")

        codes = [o.code for o in self.business_outcomes]
        if len(codes) != len(set(codes)):
            raise ValueError("business outcome codes must be unique")

        return self

    # -- the agent-facing contract -----------------------------------------

    def input_schema(self) -> dict[str, Any]:
        """JSON Schema for the arguments a caller supplies."""
        return {
            "type": "object",
            "properties": {p.name: p.json_schema() for p in self.parameters},
            "required": [p.name for p in self.parameters if p.required],
            "additionalProperties": False,
        }

    def output_schema(self) -> dict[str, Any]:
        """JSON Schema for the data a successful invocation returns."""
        return {
            "type": "object",
            "properties": {o.name: o.json_schema() for o in self.outputs},
            "required": [o.name for o in self.outputs],
            "additionalProperties": False,
        }

    def as_tool_definition(self) -> dict[str, Any]:
        """Render the capability as a callable tool.

        The point of the whole system in one method: a flow an LLM discovered
        once becomes something an agent can invoke by name, with typed
        arguments, without any model reasoning about the user interface again.
        The possible outcomes are named in the description so the calling
        agent knows in advance that "no such member" is an answer it may get.
        """
        outcomes = "".join(f"\n  - {o.code}: {o.description}" for o in self.business_outcomes)
        description = self.description
        if outcomes:
            description += f"\n\nMay return these business outcomes instead of success:{outcomes}"
        return {"name": self.id, "description": description, "input_schema": self.input_schema()}

    def sensitive_parameters(self) -> set[str]:
        return {p.name for p in self.parameters if p.sensitive}
