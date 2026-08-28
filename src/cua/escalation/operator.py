"""A minimal operator console.

Deliberately minimal, and deliberately real. The brief puts a full co-browsing
console out of scope but asks that the handoff mechanism itself not be a
sketch, so this is the smallest thing that genuinely closes the loop: it lists
what is waiting, shows an operator everything they need, hands them a link
into the live session, and records their decision back to the run that is
waiting for it.

What a production console would add is presentation -- the session embedded
rather than linked, queue routing, shift assignment, an audit trail joined to
the institution's own systems. What it would not need to add is any part of
the control-transfer model, because that lives in the engine and the queue,
not here. This is the piece that is a stand-in; the mechanism it drives is not.

    python -m cua.escalation.operator list
    python -m cua.escalation.operator show iv-1dda28007f
    python -m cua.escalation.operator take iv-1dda28007f --operator j.okafor
    python -m cua.escalation.operator release iv-1dda28007f --operator j.okafor \
        --note "Reviewed against the member's file; authorising." --authorise
"""

from __future__ import annotations

import argparse
import sys

from .broker import Handback, InterventionQueue


def _list(queue: InterventionQueue) -> int:
    waiting = queue.pending()
    if not waiting:
        print("nothing is waiting for an operator")
        return 0
    for request in waiting:
        print(f"{request.request_id}  [{request.state}]  {request.capability_id} "
              f"step {request.step_index} ({request.step_intent})  -- {request.failure_kind}")
    return 0


def _show(queue: InterventionQueue, request_id: str) -> int:
    request = queue.read(request_id)
    print(f"request      {request.request_id}   ({request.state})")
    print(f"raised       {request.raised_at:%Y-%m-%d %H:%M:%S} UTC")
    print(f"capability   {request.capability_id} v{request.capability_version}")
    print(f"goal         {request.goal}")
    print(f"stopped at   step {request.step_index}: {request.step_intent}")
    print(f"because      {request.failure_kind}")
    print(f"reason       {request.reason}")
    print(f"evidence     {request.evidence_dir}")
    if request.screenshot:
        print(f"screenshot   {request.evidence_dir}/{request.screenshot}")
    if request.snapshot:
        print(f"snapshot     {request.evidence_dir}/{request.snapshot}")
    print()
    if request.live_session_url:
        print("open this to drive the session the automation was using:")
        print(f"  {request.live_session_url}")
    else:
        print("no live session link was available; the run may already have ended")
    return 0


def _take(queue: InterventionQueue, request_id: str, operator: str) -> int:
    request = queue.claim(request_id, operator)
    print(f"{request_id} claimed by {operator}")
    if request.live_session_url:
        print(f"drive the session here: {request.live_session_url}")
    return 0


def _release(queue: InterventionQueue, request_id: str, operator: str, *,
             abandon: bool, note: str, authorise: bool, resume_from: int | None) -> int:
    handback = Handback(
        disposition="abandon" if abandon else "resume",
        operator=operator,
        note=note,
        resume_from=resume_from,
        authorise_irreversible=authorise,
    )
    queue.hand_back(request_id, handback)
    print(f"{request_id} handed back: {handback.disposition}"
          + (" (irreversible step authorised)" if authorise else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cua-operator", description=__doc__.split("\n")[0])
    parser.add_argument("--queue", default="evidence/interventions",
                        help="directory the runs raise intervention requests into")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list", help="what is waiting for an operator")

    show = commands.add_parser("show", help="everything needed to act on one request")
    show.add_argument("request_id")

    take = commands.add_parser("take", help="claim a request and get the live session link")
    take.add_argument("request_id")
    take.add_argument("--operator", required=True)

    release = commands.add_parser("release", help="hand the session back to the run")
    release.add_argument("request_id")
    release.add_argument("--operator", required=True)
    release.add_argument("--note", default="", help="what you did, in your own words")
    release.add_argument("--abandon", action="store_true",
                         help="stop the run rather than resuming it")
    release.add_argument("--authorise", action="store_true",
                         help="authorise the irreversible step this run was stopped for; "
                              "scoped to this invocation and recorded against your name")
    release.add_argument("--resume-from", type=int, default=None,
                         help="resume at a different step than the one that stopped")

    args = parser.parse_args(argv)
    queue = InterventionQueue(args.queue)

    if args.command == "list":
        return _list(queue)
    if args.command == "show":
        return _show(queue, args.request_id)
    if args.command == "take":
        return _take(queue, args.request_id, args.operator)
    return _release(queue, args.request_id, args.operator, abandon=args.abandon,
                    note=args.note, authorise=args.authorise, resume_from=args.resume_from)


if __name__ == "__main__":
    sys.exit(main())
