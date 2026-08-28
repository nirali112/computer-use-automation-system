# Design write-up

## 1. Architecture

Two halves meet at one artifact. **Discovery** is expensive, non-deterministic
and runs once per capability: a model drives a live surface until a goal is met.
**Replay** is cheap, deterministic and runs in production: no model is consulted,
ever. The capability artifact is the entire interface between them, which is why
most decisions below are really decisions about the artifact.

The load-bearing decision is that **`Observation` is a value, not a live
handle**. A surface reports what an operator could see — roles, accessible names,
the text beside unlabelled controls, grids — and everything above works on that
snapshot. Two things follow: the logic most likely to operate the wrong record is
testable exhaustively without a browser, and a new surface need only produce an
`Observation` to inherit every strategy, assertion and extraction unchanged.

The second is perceiving through the **accessibility tree** rather than the DOM,
and acting by **invoking the node it returns** rather than clicking a coordinate.
Role, name and value are what Windows UIA and macOS AX expose over native
widgets; `invoke()` is named after `InvokePattern.Invoke()`. It also removes
scrolling and overlays as sources of flake. The cost is blindness to anything
the tree omits — for a canvas-rendered app the answer is a different `Surface`,
not a change above it.

Simplicity over infrastructure throughout: one process, files on disk, no queue,
no database.

## 2. Artifact schema

Artifacts are JSON files on disk because they are *reviewable documents* — a
change to a capability appears as a diff in code review, the cheapest approval
workflow an institution will actually use.

A capability is a **contract**, not a recording. A step list says what happened
once; a contract says what a caller may ask for, what it gets back, which
non-success answers are legitimate, and how anyone can tell whether the flow
worked. Only a contract is safe to invoke unattended.

Beyond ordered steps it declares typed **parameters** and **outputs**, a
**checkpoint**, **business outcomes**, **recovery** rules and **failure
signals**. `as_tool_definition()` renders it as a callable tool whose description
names the outcomes, so a calling agent knows in advance that `MEMBER_NOT_FOUND`
is an answer it may receive.

Three specifics worth defending:

**Steps bind parameter references, never values.** One decision, two payoffs: it
makes a recording reusable rather than a transcript of one run, and no credential
is ever written into an artifact — there is nothing stored to redact.

**A target is an ordered chain of strategies, not a locator.** The application
forced this: its search field resolves by role and accessible name, while its
sub-account form's two inputs both report an *empty* name and are
distinguishable only by the adjacent cell. One strategy provably cannot serve
both. Each carries its own rationale and confidence, and replay reports which
resolved — so a capability quietly surviving on its last resort is visible.

**Business outcomes are declared in the artifact**, making "no such member is an
answer, not a crash" a versioned, reviewable property of the capability rather
than an accident of whoever wrote the exception handler.

The schema validates itself: a step binding an undeclared parameter,
non-consecutive indices, duplicate outcome codes, or a sensitive parameter
carrying an example are rejected at load time. That last rule came from a test —
an example is documentation, documentation gets committed, and a sample
credential in a reviewed file is a leak. The trade-off is a larger artifact; the
alternative is a smaller one whose meaning lives in code nobody reviews.

## 3. Determinism & error handling

Determinism is not recorded timings reproduced — that is what makes recorded
automation flaky. It is three refusals. **Nothing waits for a duration**: every
wait is for an observable condition, so a step completes when the surface is
ready and fails with a *named* unmet expectation when it never becomes ready.
**Nothing resolves ambiguously**: a strategy matching several controls is
skipped, never guessed at, because picking the first of three is how automation
silently opens the wrong member's record. **Nothing is assumed to have worked**:
checkpoints are asserted, so a click that did nothing fails at the step that did
nothing.

The result contract is four-way. `SUCCESS` and `BUSINESS_OUTCOME` are **both
successful replays** — `replay_worked` is written once so no caller reinvents
`status == SUCCESS` as the test for "did this work". `FAILED` means the
automation could not do its job, with a specific kind (`TARGET_UNRESOLVED`,
`CHECKPOINT_FAILED`, `EXTRACTION_FAILED`, `APPLICATION_ERROR`,
`RECOVERY_EXHAUSTED`, `UNSAFE_TO_RECOVER`, `POLICY_BLOCKED`, `INVALID_INPUT`).
`ESCALATED` means it stopped and asked for a person. Recoveries are deliberately
not a fifth status: clearing a known interstitial is something replay does
*while* succeeding, so it is recorded on the result rather than becoming it.

The loop watches four declared things at once — the expected state, a business
outcome, a recovery trigger, an application failure signal — and the first to
hold decides. **Detection is uniform; only the response differs.** That keeps the
not-found-is-an-answer rule out of an exception handler. `failure_signals` exists
because of a gap found by running it: without it an error page resolved as "the
expected state never arrived", reported ten seconds later as a timeout — slow,
and blaming the wait rather than the error on screen.

Recovery is bounded twice: by each rule's attempt limit, and by a refusal to
re-authenticate once an irreversible step has run, since replaying would submit
it twice. A duplicated transaction is far worse than a failed replay.

On drift, which the brief treats as secondary: replay already records which
strategy resolved and at what confidence, so a capability beginning to resolve
via a lower-confidence fallback *is* the drift signal, with no new machinery.

## 4. Heterogeneity & multi-tenant

**The surface seam.** `surfaces/base.py` is the intersection of what web
accessibility trees and platform accessibility APIs both provide: role, name,
value, contextual labels, grids, invoking a control. No selectors, XPath or DOM
types appear in it or anything consuming it. A legacy web app is the *same*
implementation with worse names, which simply shifts more targets onto the
adjacent-cell strategy. A desktop app is a new `Surface` mapping UIA/AX to the
same `Observation`; artifact, resolver, replay and escalation are unchanged.

Enforced, not intended: `tests/test_seam.py` imports the artifact schema,
resolver and replay engine in a clean interpreter and fails if a browser comes
with them. A design claim decays quietly — one convenience import during a
debugging session and the abstraction is fiction while still passing review.

The honest limit: a screenshot-and-coordinates surface needs a new member of the
strategy union, since nothing in the schema is positional in pixel space. The
schema accommodates it; it does not contain it.

**Multi-tenant.** A capability is recorded against a *product*
(`surface.application` plus version), not a tenant — which is why `Surface`
carries `application`, `tenant` and `variant_of` rather than a bare URL. The
model is a shared base plus **sparse per-tenant overrides**: a tenant that brands
the vendor product differently stores only what differs, so a fix to the base
propagates instead of being re-applied a hundred times.

Two details are designed, not built. Overrides must key on a **stable step
identifier**, not an index, because indices move whenever the base is
re-recorded — the review scripts locate steps by intent for exactly this reason.
And the *number* of overrides is itself a signal: past a threshold a tenant is
not a variant, and pretending otherwise produces a capability nobody can reason
about.

**Drift** is detected from evidence the system already emits: which strategy
resolved, at what confidence, how often recovery fired. A per-tenant canary
replay compared against that profile catches a vendor upgrade before a real
invocation does. Divergence opens a review; it never auto-repairs.

## 5. Escalation & handoff

The requirement is worded precisely — the operator takes over *"the same live
session the automation was using, not a fresh one"* — and the design follows from
that. A second browser would lose the signed-on session, the half-completed form
and the history, which is most of what an operator needs. So the browser runs
with a debugging port and the request carries a URL onto **that tab**, served
locally so an operator on an institution's network needs no internet.

"Stuck" is not a heuristic: it is any failure of an escalatable kind — an
unresolvable control, a checkpoint that never held, a recovery out of attempts,
an application error, a blocked step. Deliberately *not* escalatable: a malformed
argument, which no operator attention fixes. Escalating what nobody can resolve
trains operators to dismiss requests, which is how a working escalation path
quietly stops working.

Control is a token with one holder, and the surface is **wrapped** so acting
without it raises rather than being discouraged — that rule is exactly the kind
that survives review and then fails at three in the morning when a retry path
forgets to check. Observing stays permitted: watching is not acting, and
blinding the automation would make an independent record of the operator's work
impossible. So a handback carries what the operator decided, what they say they
did, and separately what the automation observed changing while it watched.

One field earns its place: an operator can **authorise an irreversible step** as
part of handing back, scoped to that invocation and recorded against their name —
the brief's "require confirmation", made concrete.

Requests are files in a directory: inspectable with `ls`, surviving a crash of
either side, needing no broker. Swapping in a real queue changes one class. The
operator console is a CLI; a production one would add presentation and routing,
and no part of the control-transfer model, which lives in the engine and the
queue.

## 6. Safety

The allowlist is a **YAML file**, so someone who is not an engineer can read and
change it, enforced **at the action boundary** on both execution paths — "must
not act outside the allowlist" is a statement about every action, not an
intention. It controls origins *and* routes: on a servicing console, reading a
member and administering the institution share a host.

The discovery agent's tool surface is *built from* the policy, so a forbidden
action is never offered — stronger than refusing afterwards, since a refusal the
model never sees is one it cannot work around. The execution check remains: a
tool surface shapes what is likely, a guardrail must handle what is possible.

Irreversible steps get three gates: policy must permit them, the capability must
be **approved**, and the caller must authorise them **per invocation** — or a
named operator authorises them on the live session. Three, because the failure
modes are not symmetric. A blocked transfer is an inconvenience resolved in
minutes; an unintended one is money that has moved against a real member's
account, discovered later. Where costs are that lopsided, the default belongs on
the recoverable side.

Redaction has two independent layers, because one mechanism means one mistake is
enough. Structurally, sensitive values are never stored: steps hold parameter
references, and sensitive outputs become secrets the moment they are read. By
pattern, captured text is scrubbed of SSNs, dates of birth, emails and card
numbers — data the automation never handled but that was on screen when a
snapshot was taken. Nothing sensitive appears anywhere in `evidence/`; checked,
not asserted.

**Three limits, named rather than left to be found.** Pattern redaction
recognises *shapes, not meanings* — it will never catch a member's name, which is
why the structural layer exists. A failure **screenshot** necessarily shows
whatever was on screen; in production it would be encrypted at rest with short
retention. And a **discovery run can perform an irreversible action**, because
nothing has classified it yet — risk is an output of review, not an input to
discovery — so recording must run against a non-production instance.

## 7. Cuts

**Cut on purpose, seam left real:**

- **Desktop and screenshot surfaces.** The `Surface` protocol is the deliverable,
  enforced by a test. Nothing above it would change.
- **Multi-tenant override resolution.** Designed in §4, present in the schema, no
  resolver written. Building tenant plumbing before a second tenant exists is the
  infrastructure the brief warns against.
- **The operator console.** A CLI, not co-browsing. The control transfer it
  drives is real.
- **Outcome discovery.** A run that succeeds cannot observe what happens
  otherwise, so outcomes, recovery and risk classification are added at review.
  Visible in `evidence/`: runs 05 and 06 are the same unknown member against the
  draft and the reviewed capability.
- **Concurrency, queues, persistence.** One process, files on disk.

**Next, in order:** probe runs that deliberately provoke each error path and
record its signature — the largest gap between what this does and what it should;
stable step keys and the override resolver, making the multi-tenant story
executable rather than designed; a per-tenant canary schedule using the
confidence data replay already emits; and a second surface — a small desktop
app — because the seam is argued rather than demonstrated until something else
satisfies it.

**What I would not build**, and would push back on being asked to: automatic
repair of a drifted capability. The system detects drift and opens a review. A
system that silently adapts to a changed screen will eventually adapt to the
wrong one, inside software that moves money.
