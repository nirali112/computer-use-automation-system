"""Interpreting what an artifact declares against what a surface reports.

Three jobs, all of them pure functions from an `Observation` to an answer:

    resolve_target      which control does this recorded target mean?
    evaluate_condition  does this checkpoint, outcome detector or recovery
                        trigger hold right now?
    extract_value       what value does this output's extraction produce?

Keeping them pure is a deliberate design choice with two payoffs. The obvious
one is that all of it is testable against hand-built observations, with no
browser and no timing. The subtler one is that it forces the surface layer to
report everything a decision could need up front -- so a new surface only has
to produce an `Observation`, and inherits every targeting strategy, every
assertion kind and every extraction for free.

Nothing here imports a browser, and nothing here knows what a handle means.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .artifact.capability import AdjacentCell, ControlText, TableCell
from .artifact.conditions import Condition, ControlPresent, TextAbsent, TextPresent
from .artifact.targeting import CellAdjacent, Ordinal, RoleName, Target
from .surfaces.base import Control, Observation


def _matches(candidate: str, wanted: str, mode: str) -> bool:
    """Compare a value the surface reported against one the artifact recorded.

    `exact` means the whole value is the wanted string; `contains` means the
    wanted string appears within it, case-insensitively. Accessible names are
    compared with `exact` by default because they are computed values, not
    prose, and a loose comparison there is how a target starts matching two
    controls instead of one.
    """
    if mode == "exact":
        return candidate.strip() == wanted.strip()
    return wanted.strip().lower() in candidate.strip().lower()


def _contains_text(haystack: str, wanted: str, mode: str) -> bool:
    """Whether a body of visible text contains what an assertion is looking for.

    Distinct from `_matches`: an assertion about a screen asks whether the
    text appears somewhere in it, never whether the whole screen equals it.
    `exact` here tightens the comparison to be case-sensitive rather than
    demanding the frame contain nothing else.
    """
    if mode == "exact":
        return wanted.strip() in haystack
    return wanted.strip().lower() in haystack.lower()


# ---------------------------------------------------------------------------
# targets
# ---------------------------------------------------------------------------

@dataclass
class Attempt:
    """One strategy tried, and what came of it.

    Recorded whether it succeeded or not. A replay failure that can say
    "role and name found nothing, the adjacent-cell fallback matched three
    controls" is diagnosable from the log; one that says "element not found"
    is not.
    """

    kind: str
    confidence: str
    matched: int
    used: bool
    note: str = ""


@dataclass
class Resolution:
    control: Control | None = None
    strategy: str | None = None
    confidence: str | None = None
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.control is not None

    def describe_failure(self, target: Target) -> str:
        lines = [f"could not resolve {target.description!r} in frame {target.frame!r}:"]
        for a in self.attempts:
            lines.append(f"  {a.kind} ({a.confidence} confidence): matched {a.matched} — {a.note}")
        return "\n".join(lines)


def _candidates(strategy, controls: list[Control]) -> list[Control]:
    if isinstance(strategy, RoleName):
        return [c for c in controls if c.role == strategy.role and _matches(c.name, strategy.name, strategy.match)]
    if isinstance(strategy, CellAdjacent):
        return [
            c for c in controls
            if c.role == strategy.role
            and any(_matches(label, strategy.label_text, strategy.match) for label in c.labels)
        ]
    if isinstance(strategy, Ordinal):
        same_role = [c for c in controls if c.role == strategy.role]
        return [c for c in same_role if c.ordinal == strategy.index]
    raise TypeError(f"unhandled targeting strategy: {type(strategy).__name__}")


def resolve_target(observation: Observation, target: Target) -> Resolution:
    """Find the one control a target refers to, or explain why not.

    Strategies are tried in the order the artifact records them, and the
    first that resolves to *exactly one* control wins. A strategy matching
    several controls is skipped rather than guessed at: picking the first of
    three ambiguous matches is how automation silently operates the wrong
    record, which in this domain is worse than failing.
    """
    resolution = Resolution()
    controls = observation.controls_in(target.frame)

    for strategy in target.strategies:
        found = _candidates(strategy, controls)
        if len(found) == 1:
            resolution.attempts.append(Attempt(strategy.kind, strategy.confidence, 1, True, "resolved"))
            resolution.control = found[0]
            resolution.strategy = strategy.kind
            resolution.confidence = strategy.confidence
            return resolution
        note = "no control matched" if not found else f"ambiguous; refusing to guess between {len(found)}"
        resolution.attempts.append(Attempt(strategy.kind, strategy.confidence, len(found), False, note))

    return resolution


# ---------------------------------------------------------------------------
# conditions
# ---------------------------------------------------------------------------

@dataclass
class Evaluation:
    holds: bool
    failed: list[str] = field(default_factory=list)
    """Human-readable descriptions of the assertions that did not hold."""


def evaluate_condition(observation: Observation, condition: Condition) -> Evaluation:
    """All assertions must hold. Report every one that did not, not just the
    first, so a single failure log explains the whole gap."""
    failed: list[str] = []

    for assertion in condition.assertions:
        if isinstance(assertion, TextPresent):
            text = observation.text_in(assertion.frame)
            if not _contains_text(text, assertion.text, assertion.match):
                failed.append(
                    f"expected text {assertion.text!r} in frame {assertion.frame!r}, not found"
                )
        elif isinstance(assertion, TextAbsent):
            text = observation.text_in(assertion.frame)
            if _contains_text(text, assertion.text, assertion.match):
                failed.append(
                    f"expected text {assertion.text!r} to be absent from frame "
                    f"{assertion.frame!r}, but it is present"
                )
        elif isinstance(assertion, ControlPresent):
            if not resolve_target(observation, assertion.target).ok:
                failed.append(f"expected control {assertion.target.description!r} to be present, could not resolve it")
        else:
            raise TypeError(f"unhandled assertion: {type(assertion).__name__}")

    return Evaluation(holds=not failed, failed=failed)


# ---------------------------------------------------------------------------
# extractions
# ---------------------------------------------------------------------------

class ExtractionError(Exception):
    """An output could not be produced from what the surface reported."""


def _cell_after(rows, label_text: str, direction: str) -> str | None:
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            if cell.strip() != label_text.strip():
                continue
            if direction == "right" and c + 1 < len(row):
                return row[c + 1]
            if direction == "below" and r + 1 < len(rows) and c < len(rows[r + 1]):
                return rows[r + 1][c]
    return None


def extract_value(observation: Observation, extraction, pattern: str | None = None) -> str:
    """Produce one output's raw value, or say precisely what was missing."""
    raw: str | None = None

    if isinstance(extraction, ControlText):
        resolution = resolve_target(observation, extraction.target)
        if not resolution.ok:
            raise ExtractionError(resolution.describe_failure(extraction.target))
        raw = resolution.control.value or resolution.control.name

    elif isinstance(extraction, TableCell):
        for table in observation.tables_in(extraction.frame):
            if extraction.column_header not in table.headers:
                continue
            column = table.headers.index(extraction.column_header)
            for row in table.rows:
                if any(extraction.row_contains in cell for cell in row) and column < len(row):
                    raw = row[column]
                    break
            if raw is not None:
                break
        if raw is None:
            raise ExtractionError(
                f"no grid in frame {extraction.frame!r} had a column {extraction.column_header!r} "
                f"with a row containing {extraction.row_contains!r}"
            )

    elif isinstance(extraction, AdjacentCell):
        for table in observation.tables_in(extraction.frame):
            raw = _cell_after(table.rows, extraction.label_text, extraction.direction)
            if raw is not None:
                break
        if raw is None:
            raise ExtractionError(
                f"no cell in frame {extraction.frame!r} was labelled {extraction.label_text!r} "
                f"with a value to its {extraction.direction}"
            )
    else:
        raise TypeError(f"unhandled extraction: {type(extraction).__name__}")

    raw = raw.strip()
    if pattern:
        match = re.search(pattern, raw)
        if not match:
            raise ExtractionError(f"extracted {raw!r} but it does not match the declared pattern {pattern!r}")
        raw = match.group(1) if match.groups() else match.group(0)
    return raw


def cast_value(raw: str, value_type: str):
    """Coerce an extracted string to the type the capability declares.

    Formatting is stripped here rather than in each extraction: these screens
    render money as '$4,182.55', and a caller that declared a number should
    receive 4182.55.
    """
    if value_type == "string":
        return raw
    cleaned = raw.replace(",", "").replace("$", "").strip()
    if value_type == "integer":
        return int(cleaned)
    if value_type == "number":
        return float(cleaned)
    if value_type == "boolean":
        return cleaned.strip().lower() in {"true", "yes", "y", "1"}
    raise TypeError(f"unhandled value type: {value_type!r}")
