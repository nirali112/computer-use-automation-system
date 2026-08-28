"""The record of what a run did, and why.

Every run gets a directory. Inside it, an append-only JSONL event log is the
spine, with richer artefacts written at the moments they are actually useful
-- which in practice means when something failed, because that is when
somebody will read this without being able to reproduce the run.

Two decisions shape the format.

Events say why, not just what. "clicked a button" is not diagnosable.
"resolved 'the Initial Deposit field' by cell_adjacent at medium confidence
after role_name matched nothing" is: it says what the automation believed,
which strategy it fell back to, and therefore what changed when it stops
working.

Evidence outlives the response it describes. A replay's outputs go to the
caller who asked for them; the log is kept, copied, attached to tickets and
read by people with no business seeing a member's name. So the recorder
scrubs values it was told are sensitive from everything it writes, and the
engine never hands it a sensitive output in the first place. The two
together mean a leak needs two independent mistakes rather than one.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..safety.redact import REDACTED, Redactor

DEFAULT_ROOT = Path("evidence/runs")


class Recorder:
    """Writes one run's evidence directory."""

    def __init__(
        self,
        run_id: str,
        root: Path | str = DEFAULT_ROOT,
        secrets: set[str] | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self.run_id = run_id
        self.directory = Path(root) / run_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self._events = self.directory / "events.jsonl"
        self._redactor = redactor or Redactor(secrets)
        self._sequence = 0

    # -- redaction ---------------------------------------------------------

    def _scrub(self, value: Any) -> Any:
        return self._redactor.scrub_value(value)

    def add_secret(self, value: str) -> None:
        """Scrub this value from everything written from now on.

        Used for sensitive values that only exist once the run is under way --
        an extracted member name is not known when the recorder is created,
        but everything written after it is read must not contain it.
        """
        self._redactor.add_secret(value)

    # -- events ------------------------------------------------------------

    RESERVED = ("seq", "at", "kind")

    def event(self, kind: str, /, **fields: Any) -> None:
        """Append one event.

        `kind` is positional-only, and a field may not reuse any of the
        reserved names. Both guards exist because of the same bug caught
        twice: an event carrying its own `kind=` first raised a TypeError,
        and then, once that was silenced, quietly overwrote the event type so
        that "step_failed" was recorded as "application_error" and vanished
        from the log. A loud refusal is the only version of this that stays
        fixed.
        """
        clashes = [name for name in self.RESERVED if name in fields]
        if clashes:
            raise ValueError(
                f"event field(s) {clashes} would shadow the reserved event keys "
                f"{list(self.RESERVED)}; name them something else"
            )
        self._sequence += 1
        record = {
            "seq": self._sequence,
            "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "kind": kind,
            **self._scrub(fields),
        }
        with self._events.open("a") as handle:
            handle.write(json.dumps(record, default=str) + "\n")

    def events(self) -> list[dict]:
        if not self._events.exists():
            return []
        return [json.loads(line) for line in self._events.read_text().splitlines() if line.strip()]

    # -- richer artefacts --------------------------------------------------

    def screenshot(self, surface, label: str) -> str | None:
        """A picture of the surface. Best effort: evidence capture must never
        turn a diagnosable failure into an undiagnosable one."""
        try:
            path = self.directory / f"{label}.png"
            path.write_bytes(surface.screenshot())
            return path.name
        except Exception as error:
            self.event("evidence_capture_failed", artefact="screenshot", error=str(error))
            return None

    def snapshot(self, surface, label: str) -> str | None:
        """What the automation believed it could see.

        The signal a screenshot cannot give. A picture of the sub-account form
        shows two text boxes; only this shows that both reported an empty
        accessible name, which is the reason targeting had to fall back.
        """
        try:
            path = self.directory / f"{label}.txt"
            path.write_text(self._scrub(surface.snapshot()))
            return path.name
        except Exception as error:
            self.event("evidence_capture_failed", artefact="snapshot", error=str(error))
            return None

    def write_result(self, result: Any) -> Path:
        path = self.directory / "result.json"
        payload = asdict(result) if is_dataclass(result) else dict(result)
        path.write_text(json.dumps(self._scrub(payload), indent=2, default=str) + "\n")
        return path
