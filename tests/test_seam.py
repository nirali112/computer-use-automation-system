"""The surface seam, enforced rather than intended.

The design claim this project rests on is that the artifact schema, the
resolver and the replay engine are independent of how
any particular surface is driven -- which is what makes "this extends to a
desktop application" a structural fact rather than an assertion in a design
document.

A claim like that decays quietly. One `from playwright...` import added for
convenience during a debugging session, and the abstraction is fiction while
still looking fine in review. So it is asserted here, in the only way that
actually holds: by importing the modules in a clean interpreter and checking
that no browser came with them.
"""

import subprocess
import sys

SURFACE_INDEPENDENT_MODULES = [
    "cua.artifact",
    "cua.artifact.capability",
    "cua.artifact.targeting",
    "cua.artifact.conditions",
    "cua.artifact.steps",
    "cua.artifact.store",
    "cua.resolve",
    "cua.surfaces.base",
    "cua.replay",
    "cua.replay.engine",
    "cua.replay.outcomes",
    "cua.evidence.recorder",
    "cua.safety",
    "cua.escalation",
    "cua.escalation.control",
    "cua.escalation.broker",
    "cua.agent.synthesize",
    "cua.agent.tools",
]


def _imports_pulled_in_by(module: str) -> set[str]:
    probe = (
        f"import importlib, sys; importlib.import_module({module!r}); "
        "print('\\n'.join(sorted(sys.modules)))"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return set(result.stdout.split())


def test_the_artifact_and_resolver_do_not_depend_on_a_browser():
    for module in SURFACE_INDEPENDENT_MODULES:
        loaded = _imports_pulled_in_by(module)
        offenders = {m for m in loaded if m.split(".")[0] in {"playwright", "selenium"}}
        assert not offenders, (
            f"{module} pulled in {sorted(offenders)}. The artifact schema and the "
            f"resolver must stay independent of any one surface -- that independence "
            f"is what lets a desktop surface reuse them unchanged."
        )


def test_the_surface_protocol_itself_is_browser_free():
    """`Surface` is the contract a desktop implementation would satisfy, so it
    must be describable without the web implementation existing at all."""
    loaded = _imports_pulled_in_by("cua.surfaces.base")
    assert "cua.surfaces.web" not in loaded
