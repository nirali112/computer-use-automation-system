"""What the automation is permitted to do.

The guardrail is enforced at the action boundary rather than checked once at
the start, because "the agent must not act outside the allowlist" is a
statement about every action, not about an intention. Every navigation and
every step passes through here on both execution paths -- discovery and
replay -- so there is one place to read, and one place to get right.

Two things are controlled.

*Where* it may act: an origin allowlist plus path patterns. Origin alone is
too coarse for a servicing console, where the difference between reading a
member and administering the institution is a route.

*What* it may do: allowed action types, and separately, what happens when a
step is marked irreversible.

On irreversible actions, the brief invites a choice with a justification.
This blocks them unless the caller explicitly authorises them for that
invocation *and* the capability has been approved. The reasoning is that the
two failure modes are not symmetric. A blocked transfer is an inconvenience
someone resolves in minutes. An unintended one is money that has moved,
against a real member's account, discovered later. Where the costs are that
lopsided, the default belongs on the side that is recoverable -- and making
authorisation per-invocation rather than a stored setting means the decision
is made by whoever is asking, at the moment of asking.

Requiring approval *as well* closes the other gap: a capability that happened
to work once during discovery should not be able to move money before a human
has read what it does.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field

from ..artifact.capability import Capability
from ..artifact.steps import Step


@dataclass(frozen=True)
class Decision:
    """Whether an action may proceed, and if not, why not in plain language."""

    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


class Policy(BaseModel):
    """The configured guardrail. Loaded from a file a reviewer can read."""

    allowed_origins: list[str] = Field(
        default_factory=list,
        description="Scheme and host the automation may reach, e.g. "
        "'http://127.0.0.1:8099'. Anything else is refused outright.",
    )
    allowed_paths: list[str] = Field(
        default_factory=lambda: ["*"],
        description="Glob patterns for permitted routes. Origin alone is too "
        "coarse here: reading a member and administering the institution live "
        "on the same host.",
    )
    allowed_actions: list[str] = Field(
        default_factory=lambda: ["navigate", "click", "type", "select", "wait_for"],
        description="Action kinds the automation may perform. Enforced on both "
        "execution paths, and used to build the discovery agent's tool surface "
        "so the model is never even offered an action it may not take.",
    )
    allow_irreversible: bool = Field(
        default=False,
        description="Whether irreversible steps may run at all. Off by default; "
        "a caller still has to authorise them per invocation on top of this.",
    )
    redact_patterns: dict[str, str] | None = Field(
        default=None,
        description="Regulated-data shapes to scrub from captured text. Unset "
        "uses the built-in set.",
    )

    @classmethod
    def load(cls, path: Path | str) -> "Policy":
        return cls.model_validate(yaml.safe_load(Path(path).read_text()) or {})

    # -- where ------------------------------------------------------------

    def check_navigation(self, url: str) -> Decision:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self.allowed_origins:
            return Decision(False, f"origin {origin!r} is not on the allowlist {self.allowed_origins}")
        path = parsed.path or "/"
        if not any(fnmatch.fnmatch(path, pattern) for pattern in self.allowed_paths):
            return Decision(False, f"path {path!r} does not match any permitted route {self.allowed_paths}")
        return Decision(True)

    # -- what -------------------------------------------------------------

    def check_step(
        self,
        step: Step,
        *,
        capability: Capability,
        irreversible_authorised: bool = False,
    ) -> Decision:
        if step.action.kind not in self.allowed_actions:
            return Decision(False, f"action {step.action.kind!r} is not permitted by policy")

        if step.action.kind == "navigate":
            verdict = self.check_navigation(step.action.url)
            if not verdict:
                return verdict

        if step.risk == "irreversible":
            if not self.allow_irreversible:
                return Decision(False, "policy does not permit irreversible actions")
            if capability.approval != "approved":
                return Decision(
                    False,
                    f"capability {capability.id!r} is in {capability.approval!r} state; an "
                    f"irreversible step requires an approved capability",
                )
            if not irreversible_authorised:
                return Decision(
                    False,
                    "this step is irreversible and the caller did not authorise irreversible "
                    "actions for this invocation",
                )
        return Decision(True)


def permissive_for_testing(origin: str) -> Policy:
    """A policy that permits everything against one origin.

    Exists so tests can be explicit about running without guardrails, rather
    than quietly constructing a wide-open policy inline and leaving a reader
    to notice.
    """
    return Policy(allowed_origins=[origin], allowed_paths=["*"], allow_irreversible=True)
