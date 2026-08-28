"""The discovery run: a model driving a real application to reach a goal.

This is the expensive, non-deterministic half of the system, and it runs once
per capability. Everything it produces is consumed by the deterministic half,
so the loop's job is not just to reach the goal but to reach it in a way that
can be written down and re-executed without it.

Three things follow from that.

The model sees exactly what the replay engine resolves against -- roles,
accessible names, contextual labels -- and nothing else. It cannot choose a
control the system would be unable to find again, because it is choosing from
the list of things the system can find.

Every action is checked against the same policy that governs replay, and a
refusal comes back to the model as a tool error it can respond to. A guardrail
that only applies to the production path is not a guardrail; discovery is
where an agent is most likely to wander somewhere it should not.

The run is bounded -- by steps, by wall clock, and by the model's own budget.
An unbounded agent loop against a live banking system is not a research
inconvenience; it is the thing that must never happen.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic

from ..evidence.recorder import Recorder
from ..safety.policy import Policy
from ..surfaces.base import Control, Observation, Surface
from .tools import render, tools_for

MODEL = "claude-opus-5"
MAX_STEPS = 40
MAX_SECONDS = 300
MAX_TOKENS = 8_000

SYSTEM = """\
You are operating a back-office banking application through its accessibility \
tree, the way an assistive technology would. You are recording a procedure: \
this run will be turned into an automation that repeats it without you, so \
prefer the plainest reliable path over a clever one.

How you see the screen. After every action you are given the current state: the \
visible text of each frame, and a numbered list of the controls on it. The \
numbers are only valid for the listing you were just given -- they change every \
turn, so always read the newest listing before acting.

How you act. Address a control by its number. Never guess at a control that is \
not in the listing, and never assume an action worked -- check the next state.

Some controls have no accessible name at all; those show nearby-text taken from \
the surrounding layout, which is what tells you what the field is for. This is \
normal in these applications, not a fault.

Working rules:
- Take one action at a time and read the result before the next one.
- If something unexpected appears -- a notice, an error, a login screen you did \
not expect -- deal with what is actually on the screen rather than repeating \
your previous action.
- If an action is refused by policy, do not try to work around it. Either find \
a permitted route to the goal or stop and say so.
- Call finish as soon as the goal is met, and quote the values you were asked \
for exactly as they appear.
- Call give_up if you reach a dead end. Stopping with a clear reason is a good \
outcome; flailing is not.
"""


@dataclass
class RecordedAction:
    """One action the model took, and the control it took it on.

    The control is kept whole rather than reduced to a description, because
    synthesis derives the targeting strategies from what the control actually
    reported -- its name if it had one, its neighbouring text if it did not.
    """

    kind: str
    why: str
    control: Control | None = None
    value: str | None = None
    url: str | None = None
    observation_before: Observation | None = None
    """The screen the control was chosen from. Synthesis needs it to check
    that a derived targeting strategy actually resolves uniquely there, so
    the recorded confidence is a measurement rather than a guess."""

    observation_after: Observation | None = None


@dataclass
class DiscoveryRun:
    goal: str
    outcome: str = "exhausted"  # "completed" | "gave_up" | "exhausted" | "timed_out"
    actions: list[RecordedAction] = field(default_factory=list)
    finish_payload: dict[str, Any] | None = None
    give_up_reason: str | None = None
    final_observation: Observation | None = None
    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    duration_ms: int = 0

    @property
    def succeeded(self) -> bool:
        return self.outcome == "completed"

    def estimated_cost_usd(self) -> float:
        """Rough cost of this run, printed so it is never a surprise.

        Uses list prices for the model; cached input is charged at a tenth.
        Approximate on purpose -- it exists to keep an experiment honest, not
        to reconcile a bill.
        """
        fresh = max(self.input_tokens - self.cached_tokens, 0)
        return (fresh * 5.0 + self.cached_tokens * 0.5 + self.output_tokens * 25.0) / 1_000_000


class DiscoveryAgent:
    """Drives a surface with a model until the goal is met or a bound is hit."""

    def __init__(
        self,
        surface: Surface,
        recorder: Recorder,
        policy: Policy,
        *,
        client: anthropic.Anthropic | None = None,
        model: str = MODEL,
        max_steps: int = MAX_STEPS,
        max_seconds: float = MAX_SECONDS,
        effort: str = "high",
    ) -> None:
        self.surface = surface
        self.recorder = recorder
        self.policy = policy
        self.client = client or anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self.model = model
        self.max_steps = max_steps
        self.max_seconds = max_seconds
        self.effort = effort

    # -- executing what the model asked for --------------------------------

    def _act(self, name: str, args: dict[str, Any], observation: Observation) -> tuple[str, RecordedAction | None]:
        """Perform one tool call. Returns what to tell the model, and what to record."""
        if name == "navigate":
            url = args["url"]
            verdict = self.policy.check_navigation(url)
            if not verdict:
                return f"Refused by policy: {verdict.reason}", None
            self.surface.navigate(url)
            return "Navigated.", RecordedAction(kind="navigate", why="open the application", url=url)

        if name not in self.policy.allowed_actions:
            return f"Refused by policy: action {name!r} is not permitted.", None

        try:
            control = observation.controls[int(args["control"])]
        except (IndexError, ValueError, KeyError):
            return (
                f"There is no control {args.get('control')!r} in the listing you were given. "
                f"Read the current listing and try again."
            ), None

        if name == "click":
            self.surface.invoke(control)
            return "Clicked.", RecordedAction(kind="click", why=args["why"], control=control)
        if name == "type":
            self.surface.enter_text(control, args["text"])
            return "Typed.", RecordedAction(kind="type", why=args["why"], control=control,
                                            value=args["text"])
        if name == "select":
            try:
                self.surface.choose_option(control, args["option"])
            except Exception as error:
                return f"That option could not be chosen: {error}", None
            return "Selected.", RecordedAction(kind="select", why=args["why"], control=control,
                                               value=args["option"])
        return f"Unknown action {name!r}.", None

    # -- the loop ----------------------------------------------------------

    def run(self, goal: str, entry_point: str, inputs: dict[str, str] | None = None) -> DiscoveryRun:
        started = time.monotonic()
        run = DiscoveryRun(goal=goal)
        inputs = inputs or {}
        tools = tools_for(self.policy)

        self.recorder.event("discovery_started", goal=goal, entry_point=entry_point,
                            model=self.model, max_steps=self.max_steps,
                            offered_tools=[t["name"] for t in tools])

        observation = self.surface.observe()
        opening = (
            f"Goal: {goal}\n\n"
            f"Start at: {entry_point}\n"
        )
        if inputs:
            supplied = "\n".join(f"  {k} = {v}" for k, v in inputs.items())
            opening += (
                f"\nUse exactly these values where the application asks for them. They are "
                f"the inputs a caller will supply each time this procedure is repeated, so "
                f"type them verbatim:\n{supplied}\n"
            )
        opening += f"\nCurrent state:\n{render(observation)}"

        messages: list[dict[str, Any]] = [{"role": "user", "content": opening}]

        while run.steps < self.max_steps:
            if time.monotonic() - started > self.max_seconds:
                run.outcome = "timed_out"
                break

            with self.client.messages.stream(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=[{"type": "text", "text": SYSTEM,
                         # The system prompt and tool list are identical on every
                         # turn, so they are cached; only the growing transcript
                         # is charged at full price.
                         "cache_control": {"type": "ephemeral"}}],
                tools=tools,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                messages=messages,
            ) as stream:
                response = stream.get_final_message()

            run.input_tokens += response.usage.input_tokens
            run.output_tokens += response.usage.output_tokens
            run.cached_tokens += getattr(response.usage, "cache_read_input_tokens", 0) or 0
            messages.append({"role": "assistant", "content": response.content})

            calls = [block for block in response.content if block.type == "tool_use"]
            if not calls:
                messages.append({"role": "user", "content":
                                 "Take an action, or call finish or give_up."})
                continue

            results: list[dict[str, Any]] = []
            stop = False
            for call in calls:
                if call.name == "finish":
                    run.finish_payload = dict(call.input)
                    run.outcome = "completed"
                    stop = True
                    break
                if call.name == "give_up":
                    run.give_up_reason = call.input.get("reason", "")
                    run.outcome = "gave_up"
                    stop = True
                    break

                run.steps += 1
                before = observation
                message, recorded = self._act(call.name, dict(call.input), observation)
                if recorded is not None:
                    recorded.observation_before = before
                    observation = self.surface.observe()
                    recorded.observation_after = observation
                    run.actions.append(recorded)
                    self.recorder.event(
                        "agent_action", step=len(run.actions) - 1, action=recorded.kind,
                        why=recorded.why,
                        control=(f"{recorded.control.role} {recorded.control.name!r}"
                                 if recorded.control else recorded.url),
                        # The value is not logged: it may be a credential, and
                        # what matters for the record is which field was filled.
                        had_value=recorded.value is not None,
                    )
                else:
                    self.recorder.event("agent_action_refused", action=call.name, told=message)

                results.append({"type": "tool_result", "tool_use_id": call.id,
                                "content": f"{message}\n\nCurrent state:\n{render(observation)}"})

            if stop:
                break
            messages.append({"role": "user", "content": results})

        run.final_observation = self.surface.observe()
        run.duration_ms = int((time.monotonic() - started) * 1000)
        self.recorder.event(
            "discovery_finished", outcome=run.outcome, steps=run.steps,
            actions=len(run.actions), duration_ms=run.duration_ms,
            input_tokens=run.input_tokens, output_tokens=run.output_tokens,
            cached_tokens=run.cached_tokens,
            estimated_cost_usd=round(run.estimated_cost_usd(), 4),
            give_up_reason=run.give_up_reason,
        )
        return run
