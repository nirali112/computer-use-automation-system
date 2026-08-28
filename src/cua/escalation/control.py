"""Who is driving.

A handoff is only meaningful if there is an answer to "who holds control right
now" that both sides agree on, and if the answer is enforced rather than
observed. Two processes politely taking turns is not a control model; it is a
race waiting for a slow page load.

So control is a token with exactly one holder, and the surface is wrapped so
that acting without the token is impossible rather than merely discouraged.
The automation cannot click something while the operator is mid-form, and the
run cannot resume until control has actually come back.

One asymmetry is deliberate: observing is always permitted, acting is not.
The automation needs to keep watching while the human works -- that is how it
records what changed, and how it knows the screen it resumes on. Watching is
not acting, and conflating the two would mean either blinding the automation
during the handoff or letting it interfere during one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from ..surfaces.base import Control, Observation, Surface


class Holder(str, Enum):
    AUTOMATION = "automation"
    HUMAN = "human"


class ControlViolation(RuntimeError):
    """The automation tried to act while a human held the session."""


@dataclass
class Transfer:
    at: datetime
    to: Holder
    reason: str


@dataclass
class SessionControl:
    """The token, and the history of its handovers."""

    holder: Holder = Holder.AUTOMATION
    transfers: list[Transfer] = field(default_factory=list)

    def cede(self, reason: str) -> None:
        """Hand the session to a human."""
        self._move(Holder.HUMAN, reason)

    def reclaim(self, reason: str) -> None:
        """Take the session back once the human is finished."""
        self._move(Holder.AUTOMATION, reason)

    def _move(self, to: Holder, reason: str) -> None:
        if self.holder is to:
            raise ControlViolation(f"control is already held by {to.value}")
        self.holder = to
        self.transfers.append(Transfer(datetime.now(timezone.utc), to, reason))

    @property
    def automation_may_act(self) -> bool:
        return self.holder is Holder.AUTOMATION


class ControlledSurface(Surface):
    """A surface that refuses to be acted on by whoever does not hold control.

    Wrapping rather than trusting. The engine could simply be careful, but
    "the automation must not act during a handoff" is exactly the kind of rule
    that survives review and then fails at three in the morning when a retry
    path forgets to check. Making it structurally impossible costs one class.
    """

    def __init__(self, inner: Surface, control: SessionControl) -> None:
        self._inner = inner
        self._control = control

    def _require_control(self, what: str) -> None:
        if not self._control.automation_may_act:
            raise ControlViolation(
                f"refusing to {what}: the session is held by {self._control.holder.value}"
            )

    # Observing is always permitted -- see the module docstring.
    def observe(self) -> Observation:
        return self._inner.observe()

    def screenshot(self) -> bytes:
        return self._inner.screenshot()

    def snapshot(self) -> str:
        return self._inner.snapshot()

    def navigate(self, url: str) -> None:
        self._require_control(f"navigate to {url}")
        self._inner.navigate(url)

    def invoke(self, control: Control) -> None:
        self._require_control(f"invoke {control.name or control.labels!r}")
        self._inner.invoke(control)

    def enter_text(self, control: Control, text: str) -> None:
        self._require_control("enter text")
        self._inner.enter_text(control, text)

    def choose_option(self, control: Control, value: str) -> None:
        self._require_control("choose an option")
        self._inner.choose_option(control, value)

    def answer_dialog(self, accept: bool) -> None:
        self._require_control("answer a dialog")
        self._inner.answer_dialog(accept)

    def close(self) -> None:
        self._inner.close()

    @property
    def live_session_url(self) -> str | None:
        """Where a human can attach to this very session."""
        return getattr(self._inner, "live_session_url", None)


def describe_change(before: Observation, after: Observation) -> str:
    """What visibly changed while somebody else was driving.

    An operator's own account of what they did is worth having and is also, on
    a bad day, incomplete or wrong. This is the independent record: what the
    session actually looked like before and after, observed by the automation
    that was watching but not acting.
    """
    notes: list[str] = []
    if before.url != after.url:
        notes.append(f"navigated from {before.url} to {after.url}")

    for frame in {f.name for f in after.frames} | {f.name for f in before.frames}:
        was, now = before.text_in(frame), after.text_in(frame)
        if was != now:
            notes.append(f"the content of frame {frame!r} changed")

    gained = {(c.role, c.name) for c in after.controls} - {(c.role, c.name) for c in before.controls}
    if gained:
        shown = ", ".join(f"{role} {name!r}" for role, name in sorted(gained)[:5])
        notes.append(f"controls appeared: {shown}")

    return "; ".join(notes) or "no observable change to the session"
