"""Asking a person for help, and getting an answer back.

An intervention request has to carry enough for somebody who was not watching
to act without going and asking. That means what was being attempted and why,
where it stopped, what the screen looked like, where the evidence is -- and,
critically, a way into the live session itself rather than a fresh one.

Requests are files in a directory. That is not a placeholder for a queue; it
is the deliberate choice for this scale. A directory of JSON is inspectable
with `ls`, survives a crash of either side, needs no broker to be running, and
makes the whole handshake legible to somebody debugging it at two in the
morning. The seam is what matters: the engine raises a request and waits for a
resolution, and swapping the directory for a real queue changes one class.

The handback carries three things. What the operator decided, so the run knows
whether to continue. What they say they did, because their account is worth
having. And, separately, what the automation observed changing while it
watched -- because an account can be incomplete, and the two together are more
trustworthy than either.

One field earns its place: an operator can authorise an irreversible step as
part of handing back. That is the "require confirmation" answer to risky
actions, made concrete -- the step stays blocked by default, and a named human
unblocks that specific invocation, having looked at it.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

DEFAULT_ROOT = Path("evidence/interventions")


@dataclass
class Intervention:
    """A request for a human to take over."""

    request_id: str
    run_id: str
    capability_id: str
    capability_version: int
    goal: str
    step_index: int | None
    step_intent: str
    reason: str
    failure_kind: str
    evidence_dir: str
    live_session_url: str | None = None
    """Where the operator attaches to the session the automation was using.

    Not a new session. The whole requirement turns on this: a fresh browser
    would lose the signed-on session, the half-completed form and the
    navigation history, which is most of what the operator needs."""

    screenshot: str | None = None
    snapshot: str | None = None
    raised_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    state: Literal["pending", "claimed", "resolved"] = "pending"

    @classmethod
    def create(cls, **kw) -> "Intervention":
        return cls(request_id=f"iv-{uuid.uuid4().hex[:10]}", **kw)


@dataclass
class Handback:
    """The operator's answer."""

    disposition: Literal["resume", "abandon"]
    operator: str
    note: str = ""
    resume_from: int | None = None
    """Where to pick up. Unset resumes at the step that stopped."""

    authorise_irreversible: bool = False
    """Whether the operator is authorising the risky step they were called for.

    Scoped to this invocation only, and recorded against their name."""

    observed_change: str = ""
    """What the automation saw change while the operator worked. Filled in by
    the engine, not the operator -- it is the independent record."""

    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class Escalator(Protocol):
    """How a run asks for help. Swappable so tests need no operator."""

    def escalate(self, intervention: Intervention) -> Handback | None:
        """Raise the request and wait. `None` means nobody came."""


class InterventionQueue:
    """A directory of intervention requests, and the answers to them."""

    def __init__(self, root: Path | str = DEFAULT_ROOT) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _request_path(self, request_id: str) -> Path:
        return self.root / f"{request_id}.json"

    def _handback_path(self, request_id: str) -> Path:
        return self.root / f"{request_id}.handback.json"

    def raise_request(self, intervention: Intervention) -> Path:
        path = self._request_path(intervention.request_id)
        path.write_text(json.dumps(asdict(intervention), indent=2, default=str) + "\n")
        return path

    def read(self, request_id: str) -> Intervention:
        raw = json.loads(self._request_path(request_id).read_text())
        raw["raised_at"] = datetime.fromisoformat(raw["raised_at"])
        return Intervention(**raw)

    def pending(self) -> list[Intervention]:
        found = []
        for path in sorted(self.root.glob("iv-*.json")):
            if path.name.endswith(".handback.json"):
                continue
            request = self.read(path.stem)
            if request.state != "resolved":
                found.append(request)
        return found

    def claim(self, request_id: str, operator: str) -> Intervention:
        """Mark a request as being worked on, so two operators do not both
        start driving the same session."""
        request = self.read(request_id)
        if request.state == "claimed":
            raise RuntimeError(f"{request_id} is already claimed")
        request.state = "claimed"
        self.raise_request(request)
        return request

    def hand_back(self, request_id: str, handback: Handback) -> Path:
        path = self._handback_path(request_id)
        path.write_text(json.dumps(asdict(handback), indent=2, default=str) + "\n")
        request = self.read(request_id)
        request.state = "resolved"
        self.raise_request(request)
        return path

    def read_handback(self, request_id: str) -> Handback | None:
        path = self._handback_path(request_id)
        if not path.exists():
            return None
        raw = json.loads(path.read_text())
        raw["at"] = datetime.fromisoformat(raw["at"])
        return Handback(**raw)


class QueueEscalator:
    """Raises a request into the queue and waits for an operator."""

    def __init__(self, queue: InterventionQueue, *, timeout_s: float = 300.0,
                 poll_s: float = 1.0) -> None:
        self.queue = queue
        self.timeout_s = timeout_s
        self.poll_s = poll_s

    def escalate(self, intervention: Intervention) -> Handback | None:
        self.queue.raise_request(intervention)
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            handback = self.queue.read_handback(intervention.request_id)
            if handback is not None:
                return handback
            time.sleep(self.poll_s)
        return None


class RecordOnlyEscalator:
    """Records the request and does not wait.

    For unattended runs where no operator is on shift: the run ends
    ESCALATED with a complete request on file, rather than blocking a worker
    for an hour on the chance somebody appears.
    """

    def __init__(self, queue: InterventionQueue) -> None:
        self.queue = queue

    def escalate(self, intervention: Intervention) -> Handback | None:
        self.queue.raise_request(intervention)
        return None
