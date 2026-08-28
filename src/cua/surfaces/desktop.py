"""A desktop surface, stubbed at the seam.

This is deliberately not implemented. It exists because the claim that the
system extends beyond a browser is worth more as a file somebody can read than
as a paragraph in a design document. Every method below names the platform API
that would satisfy it, so the remaining work is visible and bounded rather
than asserted.

The point it makes: nothing above `Surface` changes. The artifact schema, the
resolver, the replay engine, the result contract, the policy and the
escalation model are all written against `Observation`, and `Observation` was
chosen as the intersection of what web accessibility trees and platform
accessibility APIs both provide. A capability recorded here would be described
in the same roles and names as one recorded in a browser.

    concept          web (implemented)          Windows UIA          macOS AX
    ---------------------------------------------------------------------------
    control role     AX node role               ControlType          AXRole
    accessible name  AX node name               Name property        AXTitle / AXDescription
    value            AX node value              ValuePattern.Value   AXValue
    nearby text      neighbouring table cell    sibling TextBlock    sibling AXStaticText
    grid             HTML table                 TablePattern         AXTable
    invoke           element click              InvokePattern        AXPress
    enter text       focus + insertText         ValuePattern.SetValue / SendKeys
    frames           frameset frame ids         window and pane handles

Three things genuinely differ, and pretending otherwise would be the mistake:

  Frames become windows. A desktop application has top-level windows, modal
  dialogs and panes rather than frames. `Control.frame` already carries an
  opaque string, so it can name a window instead of a frame with no schema
  change, but a recorded capability is scoped to whatever that string meant.

  Navigation is not a URL. `navigate()` would launch or focus an application
  rather than fetch an address, and the policy's origin allowlist would have
  to become an executable allowlist. That is a change to `Policy`, not to the
  artifact.

  Handoff is different in kind. The web implementation hands a person a
  debugging URL onto the live tab. A desktop equivalent needs a real remote
  session -- RDP, VNC or a screen-sharing agent -- and the control token model
  is unchanged, but the mechanism for exposing the session is not something
  this seam can supply on its own.
"""

from __future__ import annotations

from .base import Control, Observation, Surface


class DesktopSurface(Surface):
    """Satisfies the `Surface` protocol without implementing it.

    Present so that the seam is checkable: a test asserts this class provides
    the whole protocol, which is the same as asserting that a real desktop
    implementation would need to change nothing above it.
    """

    PLATFORM_NOTES = {
        "observe": "UIA: TreeWalker over the automation element tree, reading ControlType, "
                   "Name and ValuePattern. macOS AX: AXUIElementCopyAttributeValues over "
                   "AXChildren. Both yield the role/name/value triple Observation carries.",
        "navigate": "Launch or focus the application rather than fetch an address. The "
                    "policy's origin allowlist becomes an executable allowlist.",
        "invoke": "UIA InvokePattern.Invoke, or AXUIElementPerformAction with AXPress. The "
                  "web implementation is already named after this.",
        "enter_text": "UIA ValuePattern.SetValue where supported, falling back to synthesised "
                      "key events. macOS AX: AXValue, or CGEvent key events.",
        "choose_option": "UIA SelectionItemPattern.Select on the combo box item.",
        "answer_dialog": "Modal dialogs are windows, so this resolves and invokes the button "
                         "on the dialog window rather than answering a browser dialog.",
        "screenshot": "Platform window capture.",
        "snapshot": "The same textual dump of what the automation could see, built from the "
                    "platform tree instead of the accessibility tree.",
    }

    def _unimplemented(self, method: str):
        raise NotImplementedError(
            f"DesktopSurface.{method} is a documented stub. It would be implemented as: "
            f"{self.PLATFORM_NOTES[method]}"
        )

    def observe(self) -> Observation:
        self._unimplemented("observe")

    def navigate(self, url: str) -> None:
        self._unimplemented("navigate")

    def invoke(self, control: Control) -> None:
        self._unimplemented("invoke")

    def enter_text(self, control: Control, text: str) -> None:
        self._unimplemented("enter_text")

    def choose_option(self, control: Control, value: str) -> None:
        self._unimplemented("choose_option")

    def answer_dialog(self, accept: bool) -> None:
        self._unimplemented("answer_dialog")

    def screenshot(self) -> bytes:
        self._unimplemented("screenshot")

    def snapshot(self) -> str:
        self._unimplemented("snapshot")

    def close(self) -> None:
        return None
