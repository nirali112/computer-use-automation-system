# Computer-Use Automation System

Record-once / replay-many automation for applications that expose no API.

An LLM drives a real user interface to accomplish a goal in natural language.
That successful run is frozen into a **typed, versioned capability artifact**,
and from then on the artifact is **replayed deterministically with no model in
the decision loop** — with an explicit error taxonomy, enforced guardrails, and
a path for handing the live session to a human when it cannot safely continue.

The design write-up is [`REPORT.md`](./REPORT.md). Runs of the whole thing,
including two genuine model-driven discovery runs, are in
[`evidence/`](./evidence/README.md).

---

## Requirements

- Python 3.11+
- Chromium, managed by Playwright (`playwright install chromium`)
- An Anthropic API key — **only** for recording a new capability. Replay,
  escalation, the guardrails and the whole test suite need no model and no
  network.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium

cp .env.example .env      # then edit it
```

`.env` holds three things:

| variable | needed for | notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | `cua discover` only | not needed to replay |
| `CUA_INPUT_OPERATOR_ID` | the demo capabilities | a sensitive parameter |
| `CUA_INPUT_OPERATOR_PASSWORD` | the demo capabilities | a sensitive parameter |

Sensitive arguments are read from the environment rather than passed on the
command line, where they would land in shell history and process listings.

`CUA_CHROMIUM_PATH` is an optional fourth: set it if your environment ships a
Chromium whose build does not match the pinned Playwright version. Leave it
unset and Playwright manages its own.

---

## The demo path

### 1. Serve the target application

The system records against **Meridian Core**, a mock back-office servicing
console included in this repository. It is deliberately built the way the real
ones are: a frameset, nested layout tables, ASP.NET-style control names, and no
test identifiers anywhere. Its sub-account form supplies no accessible names at
all, which is the case that shapes most of the design.

```bash
cua serve-mock                  # http://127.0.0.1:8099
```

Leave that running; the remaining commands need it.

### 2. Record a capability by driving the app with a model

```bash
cua discover goals/member_savings_balance.yaml
```

An LLM signs on, searches for a member, opens their record and reads the
balance. It prints what the run cost — around three cents — and saves a typed
artifact to `capabilities/`, together with a structured log and a final
screenshot under `.runs/`.

The saved capability is a **draft**. A run that succeeds observes only the path
that succeeds, so it cannot know what the console does when a member does not
exist or when the session expires. Those are added at review.

### 3. Replay it, with no model in the loop

```bash
cua replay member_savings_balance --input member_id=100234
```

```
success: returned 3 output(s): member_name, savings_account_number, savings_current_balance
  member_name = 'Dana Whitfield'
  savings_account_number = 'SAV-100234-01'
  savings_current_balance = 4182.55
```

To watch it happen rather than read about it afterwards:

```bash
cua replay member_savings_balance --input member_id=100234 --headed --slow-mo 600 --keep-open
```

`--headed` shows the browser, `--slow-mo` pauses between steps (a replay is
otherwise over in about a second, because nothing waits for a duration), and
`--keep-open` leaves the final screen up until you press Enter.

Then try a member the recording never saw — this is what parameterising the
recording buys:

```bash
cua replay member_savings_balance --input member_id=100781
```

### 4. Review the draft, then see the difference

```bash
python scripts/review_balance_capability.py     # draft v1 -> approved v2
```

Now run the same unknown member against each version. The draft fails, loudly
and with a precise diagnosis. The reviewed capability reports a business
outcome the caller can act on:

```bash
cua replay member_savings_balance --input member_id=999999
```

```
business_outcome  MEMBER_NOT_FOUND
  No member exists with the supplied ID. A legitimate answer the caller needs
  in order to act, not a failure of the automation.
```

### 5. See the whole result contract

```bash
python scripts/capture_evidence.py
```

Eleven replays covering success, business outcomes, recovery from an injected
interstitial and an expired session, an application error, a permission denial,
and a human authorising an irreversible step on the live session. Writes
`evidence/runs/` and prints a summary.

### 6. What an agent would be handed

```bash
cua catalog
cua catalog --tools
```

And an agent actually using it — asked a question in English, it reads the
catalog, calls a capability by name with typed arguments, and answers:

```bash
python scripts/agent_invokes_capability.py "What is the savings balance for member 100234?"
```

```
catalog: 2 approved capabilities
  agent invoked member_savings_balance({"member_id": "100234"})
    -> success: returned 3 output(s)
```

The credentials are deliberately absent from the schema the agent is shown. A
model asked for a password would have to be handed one to put in the argument.

---

## Running without a model

Everything except step 2 works with no API key and no network. Two capabilities
are already recorded and committed under `capabilities/`, so the replay,
escalation, guardrail and evidence paths can all be exercised offline.

```bash
pytest
ruff check .
mypy
```

145 tests, no key required; the suite starts its own copy of the mock console
and its own browser. The type check and the linter are both clean, and both
found real defects when first run — a list inferred from its first element
could not have held the fallback targeting strategy, and an autofixed import
turned out to be a re-export in use.

## Seeing the handoff yourself

Scenario 10 above runs the handoff with a stand-in operator. To be the operator:

```bash
# terminal 1 -- this will stop and wait for you for up to five minutes
cua --policy policy.escalation-demo.yaml replay open_member_subaccount \
    --input member_id=100234 --input product="Vacation Club" \
    --input opening_deposit=150.00 --input nickname="Summer Trip" \
    --escalation-timeout 300
```

It reaches the submission, refuses to take an irreversible step nobody
authorised, and waits.

```bash
# terminal 2
python -m cua.escalation.operator --queue .runs/interventions list
python -m cua.escalation.operator --queue .runs/interventions show iv-...
```

`show` prints a URL. Open it: you are attached to **the browser the automation
is using right now** — the same tab, still signed on, with the form filled in.
Look around, then hand it back:

```bash
python -m cua.escalation.operator --queue .runs/interventions release iv-... \
    --operator you --note "Checked against the member file." --authorise
```

Terminal 1 resumes and completes with a real confirmation number.

## Operator console

The commands used above:

```bash
python -m cua.escalation.operator --queue .runs/interventions list
python -m cua.escalation.operator --queue .runs/interventions show iv-...
python -m cua.escalation.operator --queue .runs/interventions release iv-... \
    --operator you --note "Reviewed and authorised." --authorise
```

`show` prints a URL that attaches to **the session the automation was already
using** — the same tab, the same signed-on session, the same half-completed
form. Not a fresh one.

---

## Layout

```
mockbank/           the target: a mock core banking servicing console
goals/              goals to record, with their typed inputs
capabilities/       saved artifacts, one file per version
policy.yaml         the guardrail: where the automation may act, and what it may do
scripts/            the review step, and the evidence capture

src/cua/
  surfaces/         THE SEAM. base.py is the interface a desktop surface would
                    also satisfy; web.py is the only implementation
  artifact/         the capability schema: typed, versioned, self-validating
  resolve.py        target resolution, checkpoints and extraction, as pure
                    functions over an observation -- no browser
  replay/           deterministic execution and the four-way result contract
  agent/            the discovery loop and artifact synthesis
  safety/           the allowlist, risk gates and redaction
  escalation/       the control token and the intervention queue
  evidence/         the structured run log
```

One structural rule holds the design together, and it is enforced by a test
rather than by good intentions: **nothing above the surface layer may import a
browser**. `tests/test_seam.py` imports the artifact schema, the resolver and
the replay engine in a clean interpreter and fails if Playwright comes with
them. That is what makes "this extends to a desktop application" a property of
the code rather than a claim in a document.
