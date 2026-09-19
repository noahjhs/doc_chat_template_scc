# User flows

End-to-end test scenarios for paths a real end user (not `harness`, not
`pytest`) actually walks through — the ones automating away routine
friction (daemon redeploys, re-pairing, etc.) stops exercising for free.
`pytest tests/` and `harness`-driven checks cover the backend's own
correctness; these cover the *actual product experience* on top of it,
including the parts no automated suite touches at all (a real download, a
real Gatekeeper prompt, a real `casper://pair` hand-off).

## When to run these

Not on every change — these are deliberate, occasional passes, not CI.
Run a flow when:
- something in its path changed (the download page, the pairing flow, the
  daemon's startup/relaunch behavior, auth_service's presence/attachment
  handling),
- or on a regular cadence regardless, specifically because nothing else
  will catch drift here otherwise.

Run against `dev` first, always. Only run against `prod` once a flow is
confirmed clean on `dev` — a first-time-setup flow in particular is
customer-facing the moment it touches `prod`.

## Format

Each flow is Preconditions → numbered Steps (Action / Expected) → Pass/Fail
criteria → Known Issues (defects this flow has already surfaced, tracked
here until fixed rather than silently worked around during the run).
Steps use `- [ ]` so a real run-through can be checked off live.

## Flows

- [first-time-setup.md](first-time-setup.md) — download → install → first
  launch → sign in → pair. The one explicitly called out when this
  directory was created: automating daemon redeploy/re-pairing removes the
  only thing that was routinely exercising this path.
