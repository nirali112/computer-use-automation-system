"""Keeping regulated data out of anything that gets written down.

Two layers, deliberately independent, because a single mechanism means a
single mistake is enough.

The first layer is structural and does most of the work: sensitive values are
never stored in the first place. Steps bind parameter references rather than
literals, so an artifact has no credential in it to leak. Sensitive outputs
are withheld from the log and registered as secrets the moment they are read.

The second layer, here, is pattern-based and catches what structure cannot:
regulated data that the automation never handled as a value but that appears
in text it captured anyway. A failure snapshot of a member detail screen
contains a name, a date of birth and a masked SSN simply because they were on
screen. No amount of care about parameters prevents that -- only scrubbing
the captured text does.

The patterns are deliberately conservative about their own limits, and it is
worth being honest about them: this recognises shapes, not meanings. It will
catch an SSN and a date of birth. It will not catch a member's name, because
a name looks like any other pair of words. Names are handled by the first
layer, by being declared sensitive on the output that reads them -- which is
exactly why both layers exist.
"""

from __future__ import annotations

import re

REDACTED = "<redacted>"

# Shapes of regulated data likely to appear on a servicing screen. Ordered
# most specific first, so a fully-formed SSN is not partially eaten by the
# masked-SSN pattern on its way past.
DEFAULT_PATTERNS: dict[str, str] = {
    "ssn": r"\b\d{3}-\d{2}-\d{4}\b",
    "ssn_masked": r"\*{3}-\*{2}-\d{4}",
    "card_number": r"\b(?:\d[ -]?){13,19}\b",
    "email": r"\b[\w.%-]+@[\w.-]+\.[A-Za-z]{2,}\b",
    "us_phone": r"\(\d{3}\)\s*\d{3}-\d{4}",
    "date_of_birth": r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b",
}

MIN_SECRET_LENGTH = 4


class Redactor:
    """Scrubs known secrets and regulated-data shapes out of text."""

    def __init__(self, secrets: set[str] | None = None, patterns: dict[str, str] | None = None) -> None:
        # Short strings are excluded on purpose: scrubbing every occurrence of
        # a two-character value would corrupt a log into uselessness, and a
        # secret that short is a different problem entirely.
        self._secrets: set[str] = {s for s in (secrets or set()) if len(s) >= MIN_SECRET_LENGTH}
        source = DEFAULT_PATTERNS if patterns is None else patterns
        self._patterns = {name: re.compile(expression) for name, expression in source.items()}

    def add_secret(self, value: str) -> None:
        """Learn a secret discovered mid-run, such as an extracted member name."""
        if value and len(value) >= MIN_SECRET_LENGTH:
            self._secrets.add(value)

    def scrub(self, text: str) -> str:
        # Known values first: an exact secret should be replaced as a whole,
        # rather than half of it being consumed by a pattern.
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, REDACTED)
        for name, pattern in self._patterns.items():
            text = pattern.sub(f"<{name}>", text)
        return text

    def scrub_value(self, value):
        """Scrub recursively through the structures an event payload can hold."""
        if isinstance(value, str):
            return self.scrub(value)
        if isinstance(value, dict):
            return {k: self.scrub_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.scrub_value(v) for v in value]
        return value
