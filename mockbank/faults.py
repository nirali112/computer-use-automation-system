"""Runtime fault injection for the mock core banking system.

The environment this project targets has stable UIs, so the interesting
failures are not layout drift -- they are the exceptional states that
legitimately occur at runtime. This module lets a test arm those states on
demand so replay behaviour against them is reproducible.

A deliberate distinction runs through the whole project:

  * Business outcomes are NOT injected here. "No record found", a validation
    error, and a permission denial are produced by the application's own
    logic, because that is what they are in reality -- legitimate answers,
    not faults.

  * This module injects only the conditions that are genuinely anomalous:
    an expired session, a transient stall, an unexpected interstitial or
    dialog, and an outright application error.

Armed faults are consumed: `arm("slow", 2)` affects the next two page
renders and then stops, which models transient conditions rather than a
permanently broken app.
"""

from dataclasses import dataclass

FAULT_KINDS = (
    "session_timeout",  # session silently expires -> re-authentication required
    "slow",             # transient stall, long enough to trip a naive timeout
    "interstitial",     # unexpected notice page interposed before the target
    "js_confirm",       # unexpected browser confirm() dialog on page load
    "app_error",        # the application itself falls over
)

SLOW_SECONDS = 6.0


@dataclass
class _Armed:
    kind: str
    remaining: int


class FaultState:
    """Faults armed for upcoming requests, consumed one render at a time."""

    def __init__(self) -> None:
        self._armed: list[_Armed] = []

    def arm(self, kind: str, count: int = 1) -> None:
        if kind not in FAULT_KINDS:
            raise ValueError(f"unknown fault kind: {kind!r}; expected one of {FAULT_KINDS}")
        self._armed.append(_Armed(kind, max(1, count)))

    def reset(self) -> None:
        self._armed.clear()

    def armed(self) -> list[dict[str, object]]:
        return [{"kind": a.kind, "remaining": a.remaining} for a in self._armed]

    def take(self, kind: str) -> bool:
        """Consume one occurrence of `kind` if armed. True when it should fire."""
        for a in self._armed:
            if a.kind == kind:
                a.remaining -= 1
                if a.remaining <= 0:
                    self._armed.remove(a)
                return True
        return False


FAULTS = FaultState()
