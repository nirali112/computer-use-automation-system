"""What the discovery model is allowed to do, and how it sees the screen.

Two decisions here carry most of the weight.

**The tool surface is built from the policy.** The model is not offered an
action the guardrail would refuse. That is a stronger arrangement than
checking afterwards: a refusal the model never sees is a refusal it cannot
argue with, cannot work around, and does not waste a turn on. The policy is
still enforced on execution as well, because a tool surface is a convenience
and a guardrail must not depend on one.

**The model addresses controls by number, not by description.** Every
observation lists what is on screen with an index, and the model says "click
7". It never writes a selector, a name to match, or an XPath.

That second one matters more than it looks. Asking a model to produce a
durable locator asks it to be good at the thing it is worst at -- guessing
which attribute will still be there next month. Asking it which control it
means asks it to be good at the thing it is best at. The system then derives
the targeting strategies from what that control actually reported, so the
artifact's robustness reasoning comes from evidence rather than from prose.
"""

from __future__ import annotations

from typing import Any

from ..safety.policy import Policy
from ..surfaces.base import Observation

MAX_FRAME_TEXT = 1800


def render(observation: Observation) -> str:
    """The screen, as the model sees it.

    Deliberately the same information the replay engine resolves against --
    roles, accessible names, contextual labels -- rather than markup. The
    model is choosing among the things the system can find again, so it
    cannot pick something replay would be unable to target.
    """
    lines = [f"url: {observation.url}", f"title: {observation.title}"]
    if observation.dialog:
        lines.append(f"a dialog appeared and was dismissed: {observation.dialog!r}")

    for frame in observation.frames:
        controls = observation.controls_in(frame.name)
        tables = observation.tables_in(frame.name)
        if not (frame.text.strip() or controls):
            continue
        lines.append(f"\n[frame {frame.name}]")
        if frame.text.strip():
            text = frame.text.strip()
            if len(text) > MAX_FRAME_TEXT:
                text = text[:MAX_FRAME_TEXT] + "\n  ...(truncated)"
            lines.append("  visible text:")
            lines.extend(f"    {line}" for line in text.splitlines() if line.strip())
        if controls:
            lines.append("  controls:")
            for index, control in enumerate(observation.controls):
                if control.frame != frame.name:
                    continue
                described = f"    [{index}] {control.role:9}"
                if control.name:
                    described += f" name={control.name!r}"
                if control.value:
                    described += f" value={control.value!r}"
                if control.labels:
                    described += f" nearby-text={list(control.labels)}"
                lines.append(described)
        for table in tables:
            if table.headers:
                lines.append(f"  grid columns={list(table.headers)} rows={len(table.rows)}")
    return "\n".join(lines)


_TOOLS: dict[str, dict[str, Any]] = {
    "navigate": {
        "name": "navigate",
        "description": "Go to a URL. Use this once at the start to open the application.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    "click": {
        "name": "click",
        "description": "Activate a control -- a button or a link -- by its index in the current listing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "control": {"type": "integer", "description": "Index from the current control listing."},
                "why": {"type": "string", "description": "What this achieves, in one short phrase."},
            },
            "required": ["control", "why"],
            "additionalProperties": False,
        },
    },
    "type": {
        "name": "type",
        "description": "Type text into a field, by its index in the current listing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "control": {"type": "integer"},
                "text": {"type": "string"},
                "why": {"type": "string"},
            },
            "required": ["control", "text", "why"],
            "additionalProperties": False,
        },
    },
    "select": {
        "name": "select",
        "description": "Choose an option in a dropdown, by the control's index and the option's visible text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "control": {"type": "integer"},
                "option": {"type": "string"},
                "why": {"type": "string"},
            },
            "required": ["control", "option", "why"],
            "additionalProperties": False,
        },
    },
}

FINISH_TOOL: dict[str, Any] = {
    "name": "finish",
    "description": (
        "Call this once the goal is met. Report what proves the goal was reached and any "
        "values the caller asked for, quoting them exactly as they appear on screen."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "success_text": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Short phrases visible on the final screen that together prove the goal was "
                    "reached. Prefer stable wording such as a heading or a column title over "
                    "anything containing a specific value."
                ),
            },
            "outputs": {
                "type": "array",
                "description": "Values the goal asked to be read back. Omit if the goal was an action.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "snake_case identifier."},
                        "description": {"type": "string"},
                        "value": {"type": "string", "description": "Exactly as shown on screen."},
                        "sensitive": {
                            "type": "boolean",
                            "description": "True if this value identifies a person.",
                        },
                    },
                    "required": ["name", "description", "value", "sensitive"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["success_text"],
        "additionalProperties": False,
    },
}

GIVE_UP_TOOL: dict[str, Any] = {
    "name": "give_up",
    "description": (
        "Call this if the goal cannot be reached -- a dead end, a refusal by the application, or "
        "no way forward from the current screen. Explain what stopped you."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"reason": {"type": "string"}},
        "required": ["reason"],
        "additionalProperties": False,
    },
}


def tools_for(policy: Policy) -> list[dict[str, Any]]:
    """The tool surface this policy permits, plus the two ways to stop.

    Building it from the policy means an action the guardrail forbids is never
    offered. The check on execution stays regardless: a tool surface shapes
    what is likely, and a guardrail has to handle what is possible.
    """
    permitted = [_TOOLS[kind] for kind in policy.allowed_actions if kind in _TOOLS]
    return [*permitted, FINISH_TOOL, GIVE_UP_TOOL]
