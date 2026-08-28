"""A `Surface` over a web application, perceived through its accessibility tree.

The perception choice is the important one. This does not read markup and does
not use selectors. It asks the browser for the accessibility tree -- the same
representation a screen reader consumes -- and works in terms of roles,
accessible names and values.

That is not a stylistic preference. Three things follow from it:

  * It works where markup does not. The target application has no test
    identifiers, ASP.NET-generated control names, and layout built from
    nested tables. Accessible names survive all of that because the browser
    computes them from labels, inner text and title attributes.

  * It is the vocabulary of platform accessibility APIs. Windows UIA and
    macOS AX expose the same role/name/value triple over native widgets, so
    a desktop implementation of this interface reports the same
    `Observation` and inherits every targeting strategy unchanged.

  * Acting on a control means invoking it, not clicking a coordinate. The
    accessibility tree hands back a node; this invokes that node, exactly as
    UIA's InvokePattern does. Screen position never enters into it, so
    scrolling, overlays and window placement cannot make a replay flaky.

Framesets force one piece of machinery. A page-level accessibility tree
contains only the root document, and in a frameset the root contains nothing
but the frames themselves. So this walks Page.getFrameTree and requests a
tree per frame, which is why `Observation` is organised by frame throughout.
"""

from __future__ import annotations

import json
import os
from typing import Any

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from .base import ROOT_FRAME, Control, FrameView, Observation, Surface, Table

# Roles worth reporting as actionable controls. Everything else in the tree is
# structure or presentation and would only make observations harder to read --
# which matters, because these observations are also what the discovery model
# is shown.
CONTROL_ROLES = {
    "button", "link", "textbox", "combobox", "checkbox", "radio",
    "searchbox", "menuitem", "tab", "listbox", "slider", "spinbutton",
}

# Collects the text that gives an unlabelled control its meaning: the cell to
# its left and the cell above it. Runs on the control's own node, so it needs
# no selector and cannot mismatch.
_LABELS_JS = """
function () {
  const out = [];
  const cell = this.closest ? this.closest('td, th') : null;
  if (cell) {
    const row = cell.parentElement;
    if (row) {
      const i = Array.prototype.indexOf.call(row.children, cell);
      if (i > 0) out.push(row.children[i - 1].innerText);
      const above = row.previousElementSibling;
      if (above && above.children[i]) out.push(above.children[i].innerText);
    }
  }
  const id = this.getAttribute && this.getAttribute('id');
  if (id) {
    const lab = document.querySelector('label[for="' + CSS.escape(id) + '"]');
    if (lab) out.push(lab.innerText);
  }
  return JSON.stringify(out.map(s => (s || '').trim()).filter(Boolean));
}
"""

# Grids, as text. Header detection is deliberately conservative: a first row
# made entirely of header cells is a header, anything else is data. Detail
# panels in this application are header-less label/value tables, and reporting
# them with empty headers is correct -- they are read by label, not by column.
_TABLES_JS = """
() => JSON.stringify(Array.from(document.querySelectorAll('table')).map(t => {
  const rows = Array.from(t.rows).map(r =>
    Array.from(r.cells).map(c => (c.innerText || '').trim()));
  const first = t.rows[0];
  const headed = first && first.cells.length &&
    Array.from(first.cells).every(c => c.tagName === 'TH');
  return { headers: headed ? rows[0] : [], rows: headed ? rows.slice(1) : rows };
}))
"""


class WebSurface(Surface):
    """Drives a browser. The only surface implemented; the seam is the point."""

    def __init__(self, *, headless: bool = True, chromium_path: str | None = None) -> None:
        self._pw = sync_playwright().start()
        path = chromium_path or os.environ.get("CUA_CHROMIUM_PATH") or None
        launch: dict[str, Any] = {"headless": headless, "args": ["--no-sandbox"]}
        if path:
            launch["executable_path"] = path
        self._browser: Browser = self._pw.chromium.launch(**launch)
        self._context: BrowserContext = self._browser.new_context(viewport={"width": 1100, "height": 720})
        self._page: Page = self._context.new_page()
        self._cdp = self._context.new_cdp_session(self._page)
        self._cdp.send("Accessibility.enable")
        self._cdp.send("DOM.enable")

        self._last_dialog: str | None = None
        self._accept_dialogs = False
        self._page.on("dialog", self._handle_dialog)

    # -- dialogs -----------------------------------------------------------

    def _handle_dialog(self, dialog) -> None:
        """Answer a modal the application raised, and remember that it did.

        A modal blocks the page until it is answered, so there is no option to
        leave one open and decide later; the only real choice is the standing
        policy, and the default is to dismiss. Dismissing is the conservative
        answer -- it is the Cancel button -- and an automation that accepts
        dialogs it did not expect is an automation that confirms irreversible
        actions nobody sanctioned. The message is reported on the next
        observation so replay can treat it as the recoverable condition it is.
        """
        self._last_dialog = dialog.message
        dialog.accept() if self._accept_dialogs else dialog.dismiss()

    def answer_dialog(self, accept: bool) -> None:
        """Set how subsequent modals are answered. See `_handle_dialog`."""
        self._accept_dialogs = accept

    # -- perception --------------------------------------------------------

    def _frame_ids(self) -> dict[str, str]:
        found: dict[str, str] = {}

        def walk(node: dict) -> None:
            frame = node["frame"]
            found[frame.get("name") or ROOT_FRAME] = frame["id"]
            for child in node.get("childFrames", []):
                walk(child)

        walk(self._cdp.send("Page.getFrameTree")["frameTree"])
        return found

    def _labels_for(self, backend_node_id: int) -> tuple[str, ...]:
        try:
            resolved = self._cdp.send("DOM.resolveNode", {"backendNodeId": backend_node_id})
            result = self._cdp.send("Runtime.callFunctionOn", {
                "objectId": resolved["object"]["objectId"],
                "functionDeclaration": _LABELS_JS,
                "returnByValue": True,
            })
            return tuple(json.loads(result["result"]["value"]))
        except Exception:
            # A node can vanish between observing the tree and asking about it.
            # Contextual labels are an enrichment, so losing them degrades
            # targeting to role and name rather than failing the observation.
            return ()

    def _controls_in(self, frame_name: str, frame_id: str) -> list[Control]:
        try:
            nodes = self._cdp.send("Accessibility.getFullAXTree", {"frameId": frame_id})["nodes"]
        except Exception:
            return []

        controls: list[Control] = []
        seen_per_role: dict[str, int] = {}
        for node in nodes:
            role = (node.get("role") or {}).get("value")
            if role not in CONTROL_ROLES or node.get("ignored"):
                continue
            backend_id = node.get("backendDOMNodeId")
            if backend_id is None:
                continue
            ordinal = seen_per_role.get(role, 0)
            seen_per_role[role] = ordinal + 1
            controls.append(Control(
                frame=frame_name,
                role=role,
                name=((node.get("name") or {}).get("value") or "").strip(),
                value=((node.get("value") or {}).get("value") or None),
                labels=self._labels_for(backend_id),
                ordinal=ordinal,
                handle=str(backend_id),
            ))
        return controls

    def _playwright_frame(self, name: str):
        if name == ROOT_FRAME:
            return self._page.main_frame
        return self._page.frame(name=name)

    def observe(self) -> Observation:
        frames: list[FrameView] = []
        controls: list[Control] = []
        tables: list[Table] = []

        for name, frame_id in self._frame_ids().items():
            pw_frame = self._playwright_frame(name)
            text = ""
            raw_tables: list[dict] = []
            if pw_frame is not None:
                try:
                    text = pw_frame.evaluate("() => document.body ? document.body.innerText : ''")
                    raw_tables = json.loads(pw_frame.evaluate(_TABLES_JS))
                except Exception:
                    pass  # a frame mid-navigation reports nothing this tick
            frames.append(FrameView(name=name, url=pw_frame.url if pw_frame else "", text=text or ""))
            controls.extend(self._controls_in(name, frame_id))
            for t in raw_tables:
                tables.append(Table(
                    frame=name,
                    headers=tuple(t["headers"]),
                    rows=tuple(tuple(r) for r in t["rows"]),
                ))

        dialog, self._last_dialog = self._last_dialog, None
        return Observation(
            url=self._page.url,
            title=self._page.title(),
            frames=tuple(frames),
            controls=tuple(controls),
            tables=tuple(tables),
            dialog=dialog,
        )

    # -- action ------------------------------------------------------------

    def _call_on(self, control: Control, function_declaration: str, *args) -> Any:
        """Run a function on the node the accessibility tree reported.

        Acting through the node handle rather than a coordinate is what makes
        this the same shape as desktop automation, where a UIA element is
        invoked directly. It also removes an entire class of flakiness:
        nothing here depends on where the control happens to be on screen.
        """
        resolved = self._cdp.send("DOM.resolveNode", {"backendNodeId": int(control.handle)})
        return self._cdp.send("Runtime.callFunctionOn", {
            "objectId": resolved["object"]["objectId"],
            "functionDeclaration": function_declaration,
            "arguments": [{"value": a} for a in args],
            "returnByValue": True,
        })

    def _settle(self) -> None:
        try:
            self._page.wait_for_load_state("load", timeout=10_000)
        except Exception:
            pass

    def navigate(self, url: str) -> None:
        self._page.goto(url, wait_until="load")

    def invoke(self, control: Control) -> None:
        self._call_on(control, "function () { this.scrollIntoView({block:'center'}); this.click(); }")
        self._settle()

    def enter_text(self, control: Control, text: str) -> None:
        self._call_on(control, "function () { this.scrollIntoView({block:'center'}); this.focus(); this.value = ''; }")
        self._cdp.send("Input.insertText", {"text": text})

    def choose_option(self, control: Control, value: str) -> None:
        self._call_on(control, """
            function (wanted) {
              const option = Array.from(this.options).find(
                o => o.value === wanted || o.text.trim() === wanted);
              if (!option) throw new Error('no such option: ' + wanted);
              this.value = option.value;
              this.dispatchEvent(new Event('change', { bubbles: true }));
            }
        """, value)

    # -- evidence ----------------------------------------------------------

    def screenshot(self) -> bytes:
        return self._page.screenshot(full_page=True)

    def snapshot(self) -> str:
        """What the automation believed it could see, as text.

        The richer failure signal a screenshot cannot provide: a picture shows
        a form, but it cannot show that both of its inputs reported an empty
        accessible name. Diagnosing a targeting failure needs the latter.
        """
        observation = self.observe()
        lines = [f"url: {observation.url}", f"title: {observation.title}", ""]
        for frame in observation.frames:
            lines.append(f"[frame {frame.name}] {frame.url}")
            controls = observation.controls_in(frame.name)
            if not controls:
                lines.append("  (no controls)")
            for c in controls:
                labels = f" labels={list(c.labels)}" if c.labels else ""
                value = f" value={c.value!r}" if c.value else ""
                lines.append(f"  {c.role:10} name={c.name!r}{value}{labels} ordinal={c.ordinal}")
            for t in observation.tables_in(frame.name):
                lines.append(f"  table headers={list(t.headers)} rows={len(t.rows)}")
            lines.append("")
        if observation.dialog:
            lines.append(f"dialog raised: {observation.dialog!r}")
        return "\n".join(lines)

    def close(self) -> None:
        try:
            self._browser.close()
        finally:
            self._pw.stop()
