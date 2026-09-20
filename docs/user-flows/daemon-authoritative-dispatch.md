# Daemon-authoritative dispatch & eval

Real, non-mocked verification of the architecture change where the daemon
alone decides allow/ask/deny (both for a real `run_shell_command`
dispatch and for `/policies/eval`) — `casper_service` never evaluates
policy itself anymore, live or mocked (see `docs/testing-strategy.md`'s
Layer 3 for what the automated suite already covers here: routing/plumbing
against a scripted `FakeDaemon`, and orchestration via explicit
`mock_tier` — neither of those exercises a real daemon's own matcher, a
real approval-resend round trip, or a real Telegram tap, which is what
this flow is for).

## Preconditions

- A real, paired daemon reachable from the account under test — the Mac
  mini's own local daemon is the natural target (see
  `reference_casper_mac_mini_topology`), already paired to a test account.
- That daemon is running the build this flow is meant to verify (confirm
  via `harness hosts list` showing `connected: true`, or check the running
  app's own version/build indicator if one exists).
- A test account (`harness login <account> --dev`) with at least one
  policy layer attached to that host, containing (or temporarily amended
  to contain) one `allow`-tier rule, one `ask`-tier rule, and no
  catch-all — see Steps 1-4 for the exact shapes needed.
- Telegram notifications linked for that account (`harness profile
  telegram-link`) with notifications enabled, for Step 5.
- `{ENV}` is `--dev` for a dev pass, `--prod` for a prod pass (see
  `docs/user-flows/README.md` — dev first, always).

## Steps

- [ ] 1. **Allow-tier dispatch.** Ensure the attached policy layer has an
      `allow`-tier rule matching e.g. `echo` (`harness policy apply` a
      small YAML, or `harness policy layer add-rule` if that exists).
      Run `harness call-tool run_shell_command --arg
      positional_args='["echo","hello"]' --host <label> {ENV}` (no
      `--mock`).
      **Expected:** `status: done`, real stdout `"hello"` in the
      aggregate — confirms the plain allow path still works with the new
      tier-aware `runRunShellCommand`.
- [ ] 2. **Ask-tier pause, resolved via `harness approvals`.** Point the
      rule (or add a second one) at `ask` tier for some other command
      (e.g. `rm`). Run `harness call-tool run_shell_command --arg
      positional_args='["rm","scratch.txt"]' --host <label> {ENV}` (no
      `--mock`, no `--approve`/`--deny`).
      **Expected:** pauses (`pending_approval`), printing an `approval_id`
      — confirms the daemon actually returned a verdict-only `Tier: "ask"`
      response (no execution) rather than either executing it or
      erroring.
      Then `harness approvals respond <approval_id> --approve {ENV}`.
      **Expected:** resolves to `done`, with a REAL (non-mock) dispatch
      result this time — confirms the resend carried `approved: true`
      through to the daemon, and the daemon actually executed on the
      resend rather than pausing again.
- [ ] 3. **Deny-tier / no matching rule.** Run `harness call-tool
      run_shell_command --arg positional_args='["definitely-not-allowed"]'
      --host <label> {ENV}`.
      **Expected:** `status: done` immediately (never pauses), output
      `"Denied by policy (no matching rule allows this call)."` — no
      daemon execution attempted beyond the one verdict round trip.
- [ ] 4. **Policy changed between ask and resolve (edge case, lower
      priority — skip under time pressure, already covered by
      `TestRunShellCommand_ApprovedFlagIgnoredWhenPolicyNowDenies` in Go).**
      Trigger an ask-tier pause as in Step 2, then before resolving it,
      edit the rule to `deny` tier (or detach the layer). Approve the
      pending approval.
      **Expected:** still denied — the approval never overrides a policy
      that's since tightened, since the daemon re-matches fresh on every
      resend rather than trusting the earlier verdict.
- [ ] 5. **`/policies/eval` against a real daemon.** `harness eval --host
      <label> --layer <layer> --arg npm --arg run {ENV}`.
      **Expected:** a real tier/matched_layer_id/matched_rule response —
      confirms the new `eval_policy` daemon action and its inline-layer
      wire format work end-to-end, not just against `tests/fake_daemon.py`.
      Also try `harness eval` with **no** `--host` while more than one
      host is connected — **expected:** a clear "specify which one"
      error, not a silent guess.
- [ ] 6. **Telegram approval, tapped for real.** Trigger another ask-tier
      pause (Step 2's shape). Confirm the Telegram message arrives with
      Approve/Deny buttons. Tap **Approve** on the phone.
      **Expected:** the conversation resumes with a real dispatch result,
      same as Step 2's harness-driven resolution — confirms the
      `/telegram/webhook` → `_resume_pending_approval` → real
      approved-resend path works with an actual webhook callback, not
      just the mocked `_FakeTelegramSend`/scripted `callback_query` the
      automated suite uses.
- [ ] 7. **Multi-account isolation spot check.** With two accounts paired
      to the same physical daemon (already set up earlier this session —
      see the multi-account pairing work), confirm an `allow`-tier rule
      attached to account A's layer does *not* let account B's otherwise-
      identical call through, and vice versa. This is a quick
      re-confirmation, not new ground — `agent/internal/commands/policy.go`
      changed substantially (the `composedRule`/`matchPolicy` signature,
      the new `eval_policy` action) even though per-identity `Handler`
      isolation itself wasn't touched by this change.

## Pass/fail

Pass = every step's actual behavior is captured, Step 4 confirms the
daemon's statelessness claim holds for real (not just in the Go unit
test), and Step 6 confirms a real Telegram round trip end-to-end at least
once since this architecture changed.

## Known issues

(none yet — first run-through pending)
