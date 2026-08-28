"""The seam between perceiving a surface and the recorded flow.

Everything above this line -- the artifact, the replay engine, the agent loop
-- is written against these types and never against a browser. Everything
below it is one implementation for one kind of surface. That is the whole of
the heterogeneity story: adding a legacy desktop application means writing
another `Surface`, not revisiting the schema or the executor.

The types are chosen to be the intersection of what web accessibility trees
and platform accessibility APIs both provide, because that intersection is
larger than it first appears:

    role, name, value        AX role/name/value; UIA ControlType/Name/Value;
                             macOS AXRole/AXTitle/AXValue
    contextual labels        the static text sitting next to an unlabelled
                             control -- available by walking siblings in a
                             DOM, and by walking the UIA/AX element tree
    tables                   HTML tables; the UIA Table and Grid patterns
    invoking a control       el.click(); UIA InvokePattern.Invoke()

Notably absent: anything to do with markup. There are no selectors, no
XPath, no CSS, and no DOM types in this module or in anything that consumes
it. A surface that has no markup at all can still satisfy this interface.

`Observation` is a value, not a live handle. It is a snapshot of what an
operator could see at one moment, which makes resolution -- deciding which
control a recorded target refers to -- a pure function over data. That is
why locator resolution can be tested exhaustively without launching a
browser at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

ROOT_FRAME = "(root)"


@dataclass(frozen=True)
class Control:
    """Something an operator could act on, as the surface reports it."""

    frame: str
    role: str
    name: str
    value: str | None = None
    labels: tuple[str, ...] = ()
    """Text that gives this control its meaning without being its name.

    For an unlabelled input in a layout table, this is the text of the
    cell to its left and the cell above it -- the two places these forms put
    a field's meaning. Matching either is correct, which is why targeting
    takes the label text alone and does not ask for a direction. It exists because the target application's
    sub-account form supplies its two inputs with no accessible name at all,
    leaving the adjacent cell as the only thing that distinguishes them.
    """

    ordinal: int = 0
    """Position among controls of the same role in the same frame.

    Only ever used as a last-resort targeting strategy, and recorded with
    low confidence when it is, because it is positional and silently changes
    meaning if a control is inserted ahead of it.
    """

    handle: str = ""
    """Opaque, surface-specific reference used to act on this control.

    Meaningless above the surface layer, and deliberately so -- nothing in
    the artifact or the replay engine may interpret it.
    """


@dataclass(frozen=True)
class Table:
    """A grid of text, however the surface happens to render one."""

    frame: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class FrameView:
    """One frame's identity and its visible text."""

    name: str
    url: str
    text: str


@dataclass(frozen=True)
class Observation:
    """What is on the surface right now."""

    url: str
    title: str
    frames: tuple[FrameView, ...] = ()
    controls: tuple[Control, ...] = ()
    tables: tuple[Table, ...] = ()
    dialog: str | None = None
    """A modal dialog the surface raised, if one appeared since the last
    observation. These arrive unannounced in the target environment, so they
    are reported as part of the state rather than as an exception."""

    def frame(self, name: str) -> FrameView | None:
        return next((f for f in self.frames if f.name == name), None)

    def text_in(self, frame: str) -> str:
        view = self.frame(frame)
        return view.text if view else ""

    def controls_in(self, frame: str) -> list[Control]:
        return [c for c in self.controls if c.frame == frame]

    def tables_in(self, frame: str) -> list[Table]:
        return [t for t in self.tables if t.frame == frame]


@dataclass
class ActionRecord:
    """One thing the surface was asked to do, for the evidence log."""

    kind: str
    detail: str
    frame: str = ROOT_FRAME
    ok: bool = True
    error: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class Surface(Protocol):
    """Perceiving and acting on one application, whatever kind it is."""

    def observe(self) -> Observation:
        """Snapshot the current state."""

    def navigate(self, url: str) -> None:
        """Go to an entry point. For a desktop surface, launch or focus it."""

    def invoke(self, control: Control) -> None:
        """Activate a control -- click a button, follow a link.

        Named for the accessibility primitive rather than for the mouse,
        because that is what it is: the web implementation invokes the
        element the accessibility tree handed back, exactly as a desktop
        implementation would call InvokePattern.Invoke on a UIA element.
        Neither needs to know where the control is on screen.
        """

    def enter_text(self, control: Control, text: str) -> None:
        """Focus a control and type into it."""

    def choose_option(self, control: Control, value: str) -> None:
        """Set a selection control to one of its options."""

    def answer_dialog(self, accept: bool) -> None:
        """Respond to a modal dialog the surface raised."""

    def screenshot(self) -> bytes:
        """A picture of the current state, for failure evidence."""

    def snapshot(self) -> str:
        """A textual dump of the current state, for failure evidence.

        The richer signal a screenshot cannot give: what the automation
        believed it could see, so a failure can be diagnosed from the
        evidence alone rather than by reproducing it.
        """

    def close(self) -> None:
        """Release the surface."""
