# Testing strategy

## High-level

Casper's policy engine exists in two independent implementations — the Go
daemon's (`agent/internal/commands/policy.go`, real filesystem/environment
access, the actual security boundary) and Python's
(`casper_service/policy.py`, no real machine access, kept alive today only
for parity coverage — see Layer 2 below). The daemon is the sole
authority: it decides allow/ask/deny for every real dispatch and every
`/policies/eval` call (`agent/internal/commands/policy.go`'s
`runRunShellCommand`/`runEvalPolicy`). `casper_service` never evaluates
policy itself, live or mocked — a mocked call takes an explicit,
caller-supplied tier (`mock_tier`) and simulates only the daemon's own
dispatch outcome, never derives one by matching anything (see
`casper_service/conversations.py`'s own module docstring).

That split is the whole strategy: **each layer owns exactly one kind of
correctness, and no layer re-tests what a lower layer already owns.**

1. **Does the daemon pick the right rule?** Go, unit-tested directly
   against `Handler.matchPolicy`/`ruleMatches`/`runRunShellCommand`/
   `runEvalPolicy` — the only place this logic actually runs.
2. **Do the two independent matchers agree with each other?** A shared
   fixture (`tests/fixtures/policy_parity.json`), loaded by both a Go test
   and a Python test, asserting they reach the same verdict on the same
   cases. Not about which one governs a live decision (only Go does) —
   about not letting them silently drift apart while Python's copy still
   exists.
3. **Does `casper_service` route/relay correctly, and orchestrate the
   conversation loop correctly?** Python, against a real HTTP daemon
   double (`tests/fake_daemon.py`) that is always handed a scripted,
   arbitrary verdict — never asked to match anything itself. This is
   where request/response wire-shape, host resolution, ownership,
   pending-approval persistence, and Telegram-notification logic get
   proven, completely decoupled from whether the *matching* itself is
   correct (Layer 1 already owns that).
4. **Does the real, end-to-end system actually work?** Manual, deliberate,
   occasional passes against a real paired daemon, a real notarized
   binary, a real Telegram tap — the things no automated layer can reach
   at all. See `docs/user-flows/` for the existing product-flow checklists
   and `docs/user-flows/daemon-authoritative-dispatch.md` (added alongside
   this doc) for the dispatch/eval/approval-resend flow specifically.

The layering is enforced by convention, not tooling — the main thing to
protect going forward is Layer 3's own discipline: **a test that wants a
tier decision must supply one explicitly (`mock_tier`, or a scripted
`FakeDaemon` response), never derive one from authored policy-layer
rules.** A Python test that creates a real policy layer/rule and expects
that rule's tier to actually govern a mocked or `FakeDaemon`-backed call is
a sign the test drifted back into re-testing Layer 1 material and should
be simplified (this happened during this refactor — see
`tests/test_conversations.py`'s history for the pattern of what got
stripped out and why).

## Detailed

### Layer 1 — Go matching semantics

**Where:** `agent/internal/commands/policy_test.go`,
`agent/internal/commands/commands_test.go`, `agent/internal/config/policy_test.go`.

**Owns:** whether a given `(positional_args, options, cwd)` call, against
a given composed set of `Rule`s, resolves to the correct
`(layer_id, rule)` — including every `Pattern` state (blank/whitelist/
blacklist, positional-length mismatches, option presence/value
requirements), `Cwd` constraints, and every `path_resolution` mode
(`.`/`$PATH`/`MANPATH`, via `resolveForMatch`). Also owns the
tier-branching behavior on top of a match: `runRunShellCommand`'s
allow/ask/deny/approved-resend logic, and `runEvalPolicy`'s inline-layer,
no-execution evaluation.

**Deliberately does NOT own:** anything about HTTP wire format, auth,
or `casper_service`'s own behavior — those are Layer 3.

**Representative tests:** `TestRuleMatches_*` (pattern states),
`TestMatchPolicy_FirstMatchWinsAcrossLayers` (composition order),
`TestRunShellCommand_AskTierPausesWithoutExecuting`/
`_ApprovedResendExecutesAskTierRule`/`_ApprovedFlagIgnoredWhenPolicyNowDenies`
(tier branching, and the "no daemon-side memory of an earlier ask" design
point), `TestEvalPolicy_MatchesInlineLayersNotTheCachedSet` (isolation
from the daemon's own attached layers).

**Add a test here when:** a new constraint type, pattern state,
`path_resolution` mode, or tier-branching rule is added anywhere in
`agent/internal/commands/policy.go`.

### Layer 2 — Go↔Python parity

**Where:** `agent/internal/commands/parity_test.go` (Go side),
`tests/test_policy_parity.py` (Python side), both loading
`tests/fixtures/policy_parity.json`.

**Owns:** that `casper_service/policy.py`'s matcher — kept alive
specifically for this cross-check, not wired into any live or mocked
request path anymore (see `casper_service/main.py`'s `eval_policy` and
`casper_service/conversations.py`'s `_dispatch_shell_command`) — still
agrees with the Go daemon's matcher on every fixture case. A
`"{roots}"`-style rule or a non-empty `path_resolution` is deliberately
excluded from the fixture (see the fixture's own `$comment`) since Python
can only ever approximate real filesystem resolution — those cases are
Go-only, Layer 1 territory.

**Add a case here when:** changing matching semantics in a way that could
plausibly diverge between the two implementations (a new `Pattern` state,
a new composition rule) — add the case to the fixture once, both tests
pick it up automatically.

### Layer 3a — `casper_service` routing/plumbing

**Where:** `tests/test_policies.py`, using `tests/fake_daemon.py`'s
`FakeDaemon` — a real, scriptable HTTP server standing in for a daemon.

**Owns:** `POST /policies/eval`'s own logic, never its matching:
host resolution (by label, ambiguity, "no host connected"), ownership
checks (can't eval another user's layer), the ad hoc layer subset
actually sent inline to the daemon (never the caller's whole layer set),
response reshaping (`matched_rule` rebuilt from the caller's own
already-fetched rule data, not re-sent by the daemon), and failure modes
(unreachable daemon → 502, stale API key → 502, no silent fallback to a
local match on any of these).

**Deliberately does NOT own:** what tier a given call resolves to —
every `FakeDaemon` response in this file is a literal, scripted
`(status, {"tier": ..., ...})`, chosen by the test author, never computed.

**Add a test here when:** `casper_service/main.py`'s `eval_policy` (or
any future daemon-routing endpoint) changes its request/response shape,
error handling, or host-resolution logic.

### Layer 3b — Conversation-loop orchestration

**Where:** `tests/test_conversations.py`, `tests/test_casper_service.py`
(the durable `pending_approvals` table + Telegram send/webhook),
`tests/test_harness_cli.py` (chat-state persistence across a kill,
cross-session approval visibility), `tests/test_harness_flows.py`
(harness `client.py` end-to-end, including the one `FakeDaemon`-backed
full-flow eval test).

**Owns:** `_drain_pending_calls`/`run_turn`'s own state machine (pause →
persist → resolve → resend-with-`approved`), pending-approval durability
and per-user scoping, Telegram notification/callback wiring (with a fake
`requests`-alike, never a real bot), multi-account/multi-user isolation,
`default_host`/ambiguous-host error surfacing.

**Deliberately does NOT own:** what tier a call resolves to — every test
here drives outcomes via `mock=True, mock_tier="allow"|"ask"|"deny"`
(default `"allow"`), never by authoring a real policy layer/rule and
hoping it matches. `_dispatch_shell_command`'s real (non-mock) HTTP path
is exercised in Layer 3a's `FakeDaemon` tests and the manual Layer 4 pass,
not here.

**Add a test here when:** the turn state machine, approval persistence,
or a notification channel's own logic changes.

### Layer 3c — Harness CLI ergonomics

**Where:** `tests/test_harness_cli.py` (Typer `CliRunner`, no real
process/socket).

**Owns:** flag parsing and defaults (`--mock-tier`, `--approve`/`--deny`,
`--host`), session/chat-state file persistence, the non-interactive
fail-clean path (`_require_flag_when_noninteractive`), REPL resilience to
a bogus command.

**Add a test here when:** a CLI command's flags, prompts, or persisted
state shape change.

### Layer 4 — Real end-to-end (manual, deliberate, not CI)

**Where:** `docs/user-flows/` (product-facing flows: download, sign-up,
pairing) and `docs/user-flows/daemon-authoritative-dispatch.md`
(allow/ask/deny/resend against a real daemon, `/policies/eval --host`
against a real daemon, a real Telegram tap).

**Owns:** everything the layers above are structurally unable to reach —
a real Gatekeeper/notarization prompt, a real `casper://pair` Apple Event,
a real daemon process actually executing (or refusing to execute) a
command, a real phone tapping a real Telegram button, real multi-account
pairing to one physical machine.

**When to run:** not on every change — see `docs/user-flows/README.md`'s
own "When to run these" section (unchanged by this doc, same reasoning
applies to `daemon-authoritative-dispatch.md`). Always against `dev`
first; `prod` only once `dev` is confirmed clean.

### Adding a test for a new feature — which layer?

- Touches matching semantics (a new constraint, pattern state, tier rule)?
  → **Layer 1** (Go), plus a **Layer 2** fixture case if it's something
  Python's approximate matcher could plausibly diverge on.
- Touches `casper_service`'s own routing to a daemon (a new
  daemon-facing endpoint, a new field on an existing one)? → **Layer 3a**,
  `FakeDaemon`-backed, scripted verdicts only.
- Touches the conversation loop, approval persistence, or a notification
  channel? → **Layer 3b**, `mock_tier`-driven, no real policy authoring.
- Touches CLI flags/output/persisted local state? → **Layer 3c**.
- Can only be observed with a real daemon, a real browser, or a real
  phone? → **Layer 4** — add a checklist entry/new doc under
  `docs/user-flows/`, don't try to force it into an automated layer it
  structurally can't live in.
