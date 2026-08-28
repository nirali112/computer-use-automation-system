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

import pytest

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


def test_a_second_surface_satisfies_the_protocol_without_changing_anything_above_it():
    """The seam, checked rather than argued.

    `DesktopSurface` is a documented stub, and that is the point: if it
    provides the whole protocol, then a real desktop implementation needs to
    change nothing in the artifact schema, the resolver, the replay engine or
    the escalation model. If someone adds a method to `Surface` that only a
    browser could satisfy, this fails.
    """
    from cua.surfaces.base import Surface
    from cua.surfaces.desktop import DesktopSurface

    required = {name for name in vars(Surface) if not name.startswith("_")}
    provided = {name for name in dir(DesktopSurface) if not name.startswith("_")}
    assert required <= provided, f"DesktopSurface is missing {sorted(required - provided)}"


def test_the_desktop_stub_says_how_each_method_would_be_implemented():
    """A stub that only raises is a TODO. One that names the platform API it
    maps to is a description of the remaining work."""
    from cua.surfaces.desktop import DesktopSurface

    surface = DesktopSurface()
    with pytest.raises(NotImplementedError, match="InvokePattern"):
        surface.invoke(None)
    with pytest.raises(NotImplementedError, match="TreeWalker"):
        surface.observe()


def test_the_desktop_stub_does_not_drag_in_a_browser_either():
    loaded = _imports_pulled_in_by("cua.surfaces.desktop")
    assert not {m for m in loaded if m.split(".")[0] in {"playwright", "selenium"}}
