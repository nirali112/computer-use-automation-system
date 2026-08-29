"""An AI agent discovering a capability and invoking it by name.

This is the stretch goal from section 8, and it is also the project's own
through-line demonstrated end to end:

    the model discovers  ->  the artifact becomes a reusable capability
                         ->  deterministic replay is how an agent invokes it

Two models appear in this script and they are doing entirely different jobs.
The one here is a *caller*: it is asked a question in English, it reads a
catalog of capabilities, and it picks one and supplies typed arguments. It
never sees the application, never chooses a control, never decides how to
drive a screen. All of that was settled once during discovery and is now
frozen in the artifact.

The second model is the one that recorded the capability, weeks ago in
production terms. It is not running. That is the entire point: invoking a
capability costs one small tool-choosing call, not a browser-driving agent
loop, and it does the same thing every time.

Note what the agent is *not* asked for. The tool schema omits the operator
credentials, because a model asked for a password would have to be given one
to put in the argument, which puts it in a prompt, a transcript and a log.
The runner supplies those from its own configuration.

    python scripts/agent_invokes_capability.py "What is the savings balance for member 100234?"
"""

from __future__ import annotations

import json
import os
import sys

import anthropic
from dotenv import load_dotenv

from cua.artifact import catalog
from cua.evidence import Recorder
from cua.replay import ReplayEngine
from cua.safety import Policy
from cua.surfaces.web import WebSurface

MODEL = "claude-opus-5"
EVIDENCE = "evidence/runs"
RUN_ID = "15-agent-invokes-capability"

SYSTEM = """\
You are an assistant for staff at a credit union. You can operate the servicing \
console through the capabilities you have been given. Use one when it answers the \
question, then reply to the person in plain language.

Some capabilities can return a business outcome instead of a result -- for example, \
that no member exists with the ID given. That is an answer, not an error: relay it \
plainly rather than retrying.
"""


def run_capability(name: str, arguments: dict, capabilities: dict) -> dict:
    """Execute a capability. No model is involved past this point."""
    capability = capabilities[name]

    # Sensitive parameters come from configuration, never from the agent.
    supplied = dict(arguments)
    for parameter in capability.parameters:
        if parameter.sensitive:
            variable = f"CUA_INPUT_{parameter.name.upper()}"
            value = os.environ.get(variable)
            if not value:
                raise SystemExit(f"{name} needs ${variable} to be set")
            supplied[parameter.name] = value

    surface = WebSurface()
    try:
        engine = ReplayEngine(surface, Recorder(f"{RUN_ID}-{name}", EVIDENCE,
                                                secrets=set(supplied.values())),
                              Policy.load("policy.yaml"))
        result = engine.run(capability, supplied)
    finally:
        surface.close()

    print(f"    -> {result.status.value}: {result.describe()}")
    if result.status.value == "success":
        return {"status": "success", "outputs": result.outputs}
    if result.status.value == "business_outcome":
        return {"status": "business_outcome", "code": result.outcome_code,
                "detail": result.outcome_description}
    return {"status": result.status.value,
            "detail": result.failure.summary() if result.failure else "no detail"}


def main() -> int:
    load_dotenv()
    question = " ".join(sys.argv[1:]) or "What is the savings balance for member 100234?"

    capabilities = {c.id: c for c in catalog() if c.approval == "approved"}
    tools = [c.as_tool_definition() for c in capabilities.values()]
    print(f"\ncatalog: {len(tools)} approved capabilities -- {', '.join(capabilities)}")
    print(f"question: {question}\n")

    client = anthropic.Anthropic()
    messages: list[dict] = [{"role": "user", "content": question}]

    for _ in range(4):
        request: dict = {
            "model": MODEL, "max_tokens": 2048, "system": SYSTEM,
            "tools": tools, "thinking": {"type": "adaptive"}, "messages": messages,
        }
        with client.messages.stream(**request) as stream:
            response = stream.get_final_message()
        messages.append({"role": "assistant", "content": response.content})

        calls = [b for b in response.content if b.type == "tool_use"]
        if not calls:
            answer = "".join(b.text for b in response.content if b.type == "text")
            print(f"\nagent's answer:\n  {answer.strip()}\n")
            return 0

        results = []
        for call in calls:
            print(f"  agent invoked {call.name}({json.dumps(dict(call.input))})")
            outcome = run_capability(call.name, dict(call.input), capabilities)
            results.append({"type": "tool_result", "tool_use_id": call.id,
                            "content": json.dumps(outcome)})
        messages.append({"role": "user", "content": results})

    print("the agent did not settle on an answer")
    return 1


if __name__ == "__main__":
    sys.exit(main())
