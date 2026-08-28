"""Command line entry points.

Four verbs, matching the four things the system does: record a capability,
run one, see what capabilities exist, and serve the target application to
record against.

    cua serve-mock
    cua discover goals/member_savings_balance.yaml
    cua replay member_savings_balance --input member_id=100234
    cua catalog
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .artifact import catalog as read_catalog, load_latest, save
from .escalation import InterventionQueue, QueueEscalator, RecordOnlyEscalator
from .evidence import Recorder
from .replay import ReplayEngine, Status
from .safety import Policy

DEFAULT_POLICY = "policy.yaml"
DEFAULT_CAPABILITIES = "capabilities"


def _surface(headless: bool = True):
    """Imported late so the artifact and replay tooling stay browser-free."""
    from .surfaces.web import WebSurface

    return WebSurface(headless=headless)


def _hold_open(args, surface) -> None:
    """Keep the browser on screen after a run, when somebody is watching.

    A finished run closes its browser immediately, which is right for the
    production path and useless for watching one. This waits for a keypress
    instead, leaving the final screen up. Skipped when nothing is attached to
    the terminal, so it can never hang a scripted or scheduled run.
    """
    if not getattr(args, "keep_open", False):
        return
    if not sys.stdin.isatty():
        print("(--keep-open ignored: nothing attached to the terminal)")
        return
    input("\nbrowser left open on the final screen. press Enter to close it. ")


def _read_inputs(spec: list[dict]) -> list:
    from .agent.synthesize import InputSpec

    resolved = []
    for entry in spec:
        if "value_from_env" in entry:
            variable = entry["value_from_env"]
            value = os.environ.get(variable)
            if not value:
                raise SystemExit(
                    f"input {entry['name']!r} reads its value from ${variable}, which is not set"
                )
        else:
            value = str(entry["value"])
        resolved.append(InputSpec(
            name=entry["name"], value=value, description=entry["description"],
            sensitive=bool(entry.get("sensitive")), pattern=entry.get("pattern"),
        ))
    return resolved


def discover(args) -> int:
    from .agent.loop import DiscoveryAgent
    from .agent.synthesize import synthesise

    request = yaml.safe_load(Path(args.goal_file).read_text())
    inputs = _read_inputs(request["inputs"])
    policy = Policy.load(args.policy)
    run_id = args.run_id or f"discover-{request['id']}-{uuid.uuid4().hex[:6]}"

    recorder = Recorder(run_id, args.evidence,
                        secrets={i.value for i in inputs if i.sensitive})
    surface = _surface(headless=not args.headed)
    try:
        agent = DiscoveryAgent(surface, recorder, policy, max_steps=args.max_steps)
        run = agent.run(request["goal"], request["entry_point"],
                        {i.name: i.value for i in inputs})

        print(f"\ndiscovery {run.outcome} in {run.duration_ms}ms, {len(run.actions)} actions")
        print(f"tokens: {run.input_tokens} in ({run.cached_tokens} cached) / "
              f"{run.output_tokens} out  --  about ${run.estimated_cost_usd():.3f}")
        if run.give_up_reason:
            print(f"the agent gave up: {run.give_up_reason}")
        if not run.succeeded:
            recorder.screenshot(surface, "discovery-ended")
            recorder.snapshot(surface, "discovery-ended")
            return 1

        recorder.screenshot(surface, "discovery-final")
        recorder.snapshot(surface, "discovery-final")
        _hold_open(args, surface)
        capability = synthesise(
            run, capability_id=request["id"], name=request["name"],
            description=request["description"], application=request["application"],
            entry_point=request["entry_point"], inputs=inputs,
            model=agent.model, run_id=run_id,
        )
    finally:
        surface.close()

    path = save(capability, args.capabilities)
    print(f"\nsaved {path}")
    print(f"  {len(capability.steps)} steps, {len(capability.parameters)} parameters, "
          f"{len(capability.outputs)} outputs, approval={capability.approval}")
    print(f"  evidence in {recorder.directory}")
    return 0


def replay(args) -> int:
    capability = load_latest(args.capability_id, args.capabilities)
    inputs = dict(pair.split("=", 1) for pair in args.input)
    # Sensitive arguments are read from the environment rather than typed on a
    # command line, where they would land in shell history and process listings.
    # The convention is one variable per parameter: CUA_INPUT_OPERATOR_PASSWORD.
    for parameter in capability.parameters:
        if parameter.sensitive and parameter.name not in inputs:
            variable = f"CUA_INPUT_{parameter.name.upper()}"
            if os.environ.get(variable):
                inputs[parameter.name] = os.environ[variable]
            else:
                raise SystemExit(
                    f"{capability.id} needs the sensitive parameter {parameter.name!r}. "
                    f"Set ${variable} rather than passing it with --input, so it stays out "
                    f"of your shell history."
                )

    policy = Policy.load(args.policy)
    run_id = args.run_id or f"replay-{capability.id}-{uuid.uuid4().hex[:6]}"
    recorder = Recorder(run_id, args.evidence,
                        secrets={inputs[p.name] for p in capability.parameters
                                 if p.sensitive and p.name in inputs})
    queue = InterventionQueue(args.interventions)
    escalator = (QueueEscalator(queue, timeout_s=args.escalation_timeout)
                 if args.escalation_timeout > 0 else RecordOnlyEscalator(queue))

    surface = _surface(headless=not args.headed)
    try:
        engine = ReplayEngine(surface, recorder, policy, escalator=escalator,
                              step_pause_ms=args.slow_mo)
        result = engine.run(capability, inputs, authorise_irreversible=args.authorise_irreversible)
        _hold_open(args, surface)
    finally:
        surface.close()

    print(f"\n{result.status.value}: {result.describe()}")
    if result.outputs:
        for name, value in result.outputs.items():
            print(f"  {name} = {value!r}")
    if result.recoveries:
        print(f"  recovered from: {[r.rule for r in result.recoveries]}")
    if result.interventions:
        print(f"  interventions raised: {result.interventions}")
    if result.failure:
        print(f"  {result.failure.summary()}")
    print(f"  evidence in {result.evidence_dir}")
    return 0 if result.replay_worked else 1


def catalog(args) -> int:
    """What an agent would be handed to decide what it can do."""
    capabilities = read_catalog(args.capabilities)
    if not capabilities:
        print(f"no capabilities in {args.capabilities}")
        return 0
    if args.tools:
        print(json.dumps([c.as_tool_definition() for c in capabilities], indent=2))
        return 0
    for capability in capabilities:
        print(f"{capability.id}  v{capability.version}  [{capability.approval}]  {capability.name}")
        print(f"    in : {', '.join(p.name for p in capability.parameters) or '-'}")
        print(f"    out: {', '.join(o.name for o in capability.outputs) or '-'}")
        for outcome in capability.business_outcomes:
            print(f"    or : {outcome.code}")
    return 0


def serve_mock(args) -> int:
    import uvicorn

    uvicorn.run("mockbank.app:app", host=args.host, port=args.port, log_level="info")
    return 0


def _add_watching(parser) -> None:
    """Options that exist only so a person can follow what is happening."""
    parser.add_argument("--slow-mo", type=int, default=0, metavar="MS",
                        help="pause this many milliseconds between steps, so a headed run "
                             "can be watched. 500 is a comfortable pace.")
    parser.add_argument("--keep-open", action="store_true",
                        help="leave the browser on the final screen until you press Enter")


def _add_common(parser, *, after_verb: bool = False) -> None:
    """Options accepted either side of the subcommand.

    `cua --headed replay ...` and `cua replay ... --headed` should both work.
    Nobody reading the second form thinks of it as a global option, and being
    told to type it backwards is a bad answer.

    Registering the same option twice needs care: argparse parses the
    subcommand last, so a plain second registration would overwrite whatever
    was given before the verb with the subparser's default. SUPPRESS stops the
    subparser setting the attribute at all unless it was actually supplied, so
    the earlier value survives and a later one still wins.
    """
    hidden = {"default": argparse.SUPPRESS} if after_verb else {}
    parser.add_argument("--policy", help="the guardrail configuration to enforce",
                        **({"default": DEFAULT_POLICY} if not after_verb else hidden))
    parser.add_argument("--capabilities", help="directory of saved capability artifacts",
                        **({"default": DEFAULT_CAPABILITIES} if not after_verb else hidden))
    parser.add_argument("--evidence", help="where run output goes; the curated set under "
                                           "evidence/ is produced by scripts/capture_evidence.py",
                        **({"default": ".runs"} if not after_verb else hidden))
    parser.add_argument("--interventions", help="directory the intervention queue lives in",
                        **({"default": ".runs/interventions"} if not after_verb else hidden))
    parser.add_argument("--headed", action="store_true", help="show the browser",
                        **({"default": False} if not after_verb else hidden))


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="cua", description=__doc__.split("\n")[0])
    _add_common(parser)
    commands = parser.add_subparsers(dest="command", required=True)

    d = commands.add_parser("discover", help="record a capability by driving the app with a model")
    _add_common(d, after_verb=True)
    d.add_argument("goal_file")
    d.add_argument("--max-steps", type=int, default=40)
    _add_watching(d)
    d.add_argument("--run-id", default=None)
    d.set_defaults(handler=discover)

    r = commands.add_parser("replay", help="run a saved capability, without a model")
    _add_common(r, after_verb=True)
    r.add_argument("capability_id")
    r.add_argument("--input", action="append", default=[], metavar="NAME=VALUE")
    r.add_argument("--run-id", default=None)
    r.add_argument("--authorise-irreversible", action="store_true",
                   help="permit this invocation to take steps that cannot be undone")
    _add_watching(r)
    r.add_argument("--escalation-timeout", type=float, default=0.0,
                   help="seconds to wait for an operator; 0 records the request and stops")
    r.set_defaults(handler=replay)

    c = commands.add_parser("catalog", help="the capabilities an agent could invoke")
    _add_common(c, after_verb=True)
    c.add_argument("--tools", action="store_true", help="print them as tool definitions")
    c.set_defaults(handler=catalog)

    s = commands.add_parser("serve-mock", help="serve the mock core banking application")
    _add_common(s, after_verb=True)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8099)
    s.set_defaults(handler=serve_mock)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
