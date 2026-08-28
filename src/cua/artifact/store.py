"""Reading and writing capability artifacts.

Artifacts are plain JSON on disk, one file per version, named
`{id}.v{version}.json`. That is a deliberate choice over a database: these
are reviewable documents. Keeping them as files means a change to a
capability shows up as a diff in code review, which is the cheapest possible
approval workflow and the one an institution will actually use.

Versions are kept rather than overwritten, so a replay that ran last month
can still be explained by the artifact it ran against.
"""

from __future__ import annotations

import json
from pathlib import Path

from .capability import SCHEMA_VERSION, Capability

DEFAULT_DIR = Path("capabilities")


class SchemaVersionError(Exception):
    """An artifact was written against a schema version this build cannot read.

    Deliberately fatal. The alternative -- accepting the fields we recognise
    and ignoring the rest -- means a capability whose meaning has changed
    would still execute, against a real banking system. Refusing is the
    conservative and correct behaviour.
    """


def save(capability: Capability, directory: Path | str = DEFAULT_DIR) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{capability.id}.v{capability.version}.json"
    path.write_text(capability.model_dump_json(indent=2, exclude_none=True) + "\n")
    return path


def load(path: Path | str) -> Capability:
    raw = json.loads(Path(path).read_text())
    found = raw.get("schema_version")
    if found != SCHEMA_VERSION:
        raise SchemaVersionError(
            f"{path} was written against artifact schema {found!r}; "
            f"this build reads {SCHEMA_VERSION!r}. Migrate the artifact rather "
            f"than loading it partially."
        )
    return Capability.model_validate(raw)


def versions(capability_id: str, directory: Path | str = DEFAULT_DIR) -> list[int]:
    directory = Path(directory)
    found = []
    for path in directory.glob(f"{capability_id}.v*.json"):
        try:
            found.append(int(path.stem.rsplit(".v", 1)[1]))
        except (IndexError, ValueError):
            continue
    return sorted(found)


def load_latest(capability_id: str, directory: Path | str = DEFAULT_DIR) -> Capability:
    available = versions(capability_id, directory)
    if not available:
        raise FileNotFoundError(f"no artifact found for capability {capability_id!r} in {directory}")
    return load(Path(directory) / f"{capability_id}.v{available[-1]}.json")


def catalog(directory: Path | str = DEFAULT_DIR) -> list[Capability]:
    """Every capability's latest version.

    This is what an agent would be handed to decide what it can do: a list of
    typed, named, described capabilities, each of which renders directly to a
    tool definition.
    """
    directory = Path(directory)
    if not directory.exists():
        return []
    ids = {path.stem.rsplit(".v", 1)[0] for path in directory.glob("*.v*.json")}
    return sorted((load_latest(i, directory) for i in ids), key=lambda c: c.id)
