# Evidence

The system running end to end: two genuine LLM-driven discovery runs, the
capabilities they produced, and eleven deterministic replays covering the whole
result contract.

Everything here was produced by running the system, not written by hand. The
replays are reproducible with `python scripts/capture_evidence.py`; the two
discovery runs are not re-run by that script, because they cost money and
involve a model, so what is committed is the evidence from the runs that
actually happened.

## The capabilities

Both were recorded by a model and then reviewed by a person. The saved
artifacts are here alongside the runs:

| file | what it is |
|---|---|
| `member_savings_balance.v1.json` | exactly as discovery produced it: a **draft** |
| `member_savings_balance.v2.json` | after review: business outcomes, recovery, **approved** |
| `open_member_subaccount.v1.json` | as discovered: a **draft** |
| `open_member_subaccount.v2.json` | after review: outcomes, and the submission marked **irreversible** |

The diff between v1 and v2 of each is what review contributes, and it is worth
reading. A run that succeeds sees only the path that succeeds, so it cannot
observe what the console does when a member does not exist, when a session
expires, or when the application falls over. Nor can it know that one of the
buttons it pressed opens an account and cannot be undone — nothing on screen
distinguishes that button from a search button. Those are facts about the
institution's business, and a person supplies them.

## The discovery runs

### `01-discovery` — reading a balance

7 actions, 20 seconds, about $0.03. The model signed on, searched for a member,
opened their record and reported the values it read.

It never wrote a locator. It saw a numbered list of controls and chose from it;
the system then derived the targeting from what each control actually reported,
and verified each derived strategy against the screen it was recorded on.

### `02-discovery-subaccount` — opening a sub-account

12 actions, 32 seconds, about $0.13. The interesting one, because this form
supplies no accessible names at all. The derived targeting shows the difference
without anybody having configured it:

```
step  7  Open the sub-account request form   role_name      high    'Open Sub-Account'
step  8  Set product type                    cell_adjacent  medium  'Product Type'
step  9  Enter opening deposit               cell_adjacent  medium  'Initial Deposit'
step 10  Enter nickname                      cell_adjacent  medium  'Account Nickname'
step 11  Submit the sub-account request      role_name      high    'Submit Request'
```

The three controls with no accessible name fell back to the text in the
adjacent cell, and the two that had one did not. That is why a target carries
an ordered chain of strategies rather than a locator.

## The replays

Each directory holds `scenario.txt` (what it demonstrates), `events.jsonl` (the
structured log of what happened and why), `result.json`, and — on failure — a
screenshot and a snapshot of what the automation could see.

| run | result |
|---|---|
| `03-replay-success` | `success` — the recorded flow, replayed without a model |
| `04-replay-member-never-recorded` | `success` — same artifact, a member discovery never saw |
| `05-replay-draft-unknown-member` | `failed` — the draft, given an unknown member |
| `06-replay-reviewed-unknown-member` | `business_outcome` — **the same input, after review** |
| `07-replay-recovers-from-interstitial` | `success` — cleared an unexpected notice and carried on |
| `08-replay-application-error` | `failed` — the console broke, and the report says so |
| `09-replay-session-expiry` | `success` — re-authenticated and replayed from the start |
| `10-escalation-irreversible-authorised` | `success` — a person authorised the write, on the live session |
| `11-replay-restricted-member` | `business_outcome` — a permission denial, reported not retried |
| `12-escalation-operator-works-the-session` | `success` — a person drove the same session by hand, then handed it back |
| `13-replay-validation-error` | `business_outcome` — a deposit below the minimum, reported not retried |
| `14-replay-unexpected-dialog` | `success` — a `confirm()` nobody asked for, dismissed and recorded |

Between them these cover every runtime condition the brief names in §3.3: a
validation error (13), a "record not found" result (06), a permission denial
(11), an unexpected dialog (14), a session timeout (09), and a failed load (08).

### The pair worth reading first

`05` and `06` are the same capability given the same unknown member ID.

Against the freshly discovered **draft**, replay fails at step 6 and says
exactly why:

```
target_unresolved at step 6 (Open member record):
  expected exactly one control matching "the 'Open' link"
  role_name     (high confidence):   matched 0 — no control matched
  cell_adjacent (medium confidence): matched 0 — no control matched
```

Against the **reviewed** capability, the same run returns:

```
business_outcome  MEMBER_NOT_FOUND
  No member exists with the supplied ID. A legitimate answer the caller
  needs in order to act, not a failure of the automation.
```

Nothing about the automation changed between those two runs. What changed is
that a person declared what the console's answer means. That is the whole
argument for the approval gate: a draft is not wrong, it is incomplete, and it
fails loudly rather than quietly guessing.

### The handoff

`10` is the requirement that the brief says must not be a TODO. A write flow
stops at its irreversible step because the caller did not authorise one. The
request carries a link into **the session the automation was already using** —
not a fresh one — and while the operator holds it the automation is
structurally unable to act on it. The operator authorises that single
submission, hands back, and the run completes with a real confirmation number.

The `events.jsonl` for that run shows the full sequence: `escalation_raised`,
then `escalation_resolved` carrying the operator's name, their own note, and
separately what the automation observed changing while it watched.

`12` is the other half of the same requirement. The console throws an error the
capability cannot recover from, so the run cedes the session. A person clears it
by hand in that same browser and says where to pick up. The automation watched
without acting throughout, so the log carries both accounts:

```
note:            Console had thrown an error page. Reloaded the console past the
                 error page; resume from the member search step.
observed_change: the content of frame 'mainFrame' changed;
                 controls appeared: button 'Search', textbox 'Member ID'
```

The second line is not the operator's word for what they did. It is what the
automation saw while it was not allowed to act.

## What is not here

No credential, no member name, no date of birth and no SSN fragment appears in
any file in this directory. That is checked, not asserted: sensitive values are
never stored structurally, and captured text is scrubbed by pattern on the way
out.

The one residual is a failure screenshot, which necessarily shows whatever was
on screen. Text redaction cannot help there, and it is a real limit rather than
an oversight — see `REPORT.md`, Safety.
