# Agent onboarding (v1)

A person points their own agent at Casper and says "go". The agent sets up
either side of peer backup on their behalf
([docs/product/scenarios/agent-onboarding.md](../product/scenarios/agent-onboarding.md)).
This is the test of the context files: a **fresh** agent with no other
knowledge of Casper, given only what a real person would give it.

## Preconditions

- A Mac with Claude Code, and no Casper installed (remove
  `/Applications/CasperGo`).
- The onboarding folder, downloaded as a person would:
  `curl -fsSL https://<casper>/onboarding.zip -o c.zip && unzip c.zip`. Or
  just the URL `https://<casper>/agents.md`.
- Answer the agent's questions in short, human-sized replies, and do the
  steps meant for the person: the restart, and the passphrase dialog.

## Steps

- [x] 1. **Sam**, in the onboarding folder: `claude`, then "go".
      **Expected:** the agent installs Casper itself and asks two questions,
      each with a suggestion (username; back up / offer space / both).
- [x] 2. Sam answers: a username, and "offer space".
      **Expected:** account created, this Mac paired, the agent connected.
      Sam is told to restart and given the exact sentence to say afterwards.
- [x] 3. Sam restarts and says that sentence.
      **Expected:** a fresh session carries on from there. It suggests an
      offering that fits the free disk space, previews it, and changes
      nothing until Sam agrees.
- [x] 4. Sam agrees, and names a friend and an amount.
      **Expected:** the offering and a single-use invite are created, and
      Sam gets a ready-to-send message with the URL and the code.
- [x] 5. **Riley**, in a separate fresh session, says exactly what Sam's
      message says to say.
      **Expected:** the agent sets up Riley's account, this Mac, and itself;
      restarts; previews redeeming the invite; and redeems it on Riley's
      yes.
- [x] 6. Riley picks a folder.
      **Expected:** a preview, then on yes the backup runs and completes.
- [x] 7. The recovery kit.
      **Expected:** a macOS dialog asks the **person** for a passphrase. The
      agent can't see or script it. The kit is saved in Documents.
- [x] 8. Riley asks to restore.
      **Expected:** a new `~/Casper Restores/<folder>-<timestamp>` that is
      byte-identical to the original.

## Pass/fail

**2026-09-30 dev run: pass.** Fresh `claude -p` sessions played each agent
on the MacBook. Both people's accounts were on that one Mac (Claude Code
isn't installed on the mini). Cross-machine transfer was already proven
2026-09-28.

- **The human side was:**
  - Sam: 4 short messages;
  - Riley: 7, including one explaining the one-Mac test setup and one after
    a bug fix (below);
  - the restart for each, and typing the passphrase once.
- **Agent usage:** about $0.55 across all sessions *at pay-as-you-go API prices*, as estimated by Claude Code's `total_cost_usd`. On a Pro/Max subscription that is usage against plan limits, not a charge.
- **The backup:** a 9 MB test folder backed up and restored byte-identical
  (SHA-256 checked). The agent also compared the restore with the original
  itself, unprompted.
- **Things the agents did well, unprompted:**
  - Sam's agent lowered its suggested offering to fit the disk (58 GB free →
    10 GB per friend).
  - Riley's agent noticed the invite pointed at *the same Mac* and paused
    to say that a same-machine backup doesn't protect anything.
  - It refused to guess whose account was whose when the Mac had two.
  - It flagged that connecting itself as Riley might replace Sam's
    connection, which was correct; see Known issues.
  - It carried the invite code through the restart in the sentence it gave
    the person.

**2026-09-30 cross-host run: pass.**
- **Sam:** a fresh Claude Code on the **Mac mini** (installed that day and
  signed in to the Principal's subscription with `claude setup-token`).
- **Riley:** a fresh Claude Code on the **MacBook**.
- Both started with Casper uninstalled; each agent installed it from the
  public download.

What happened:
- Sam's agent offered 20 GB per friend (sized to the mini's 160 GB free)
  and invited Riley with 5 GB.
- Riley's agent redeemed the invite and backed up a 9 MB folder **from the
  laptop to the mini**. The mini's disk held only 3 chunks and a manifest;
  grepping for the folder, file names and contents found nothing.
- The restore **back to the laptop** was byte-identical.
- Afterwards, Sam's agent answered "what have I shared?" on the mini:
  Riley, 5 GB, using 9 MB.

Human side:
- Sam: 6 short messages, including one after the fix below, and a restart.
- Riley: 4 messages and a restart.

Two notes on this run:
- **The recovery-kit dialog went unanswered** (nobody was at the screen).
  It gave up after 3 minutes, and the agent explained and offered to
  retry: a graceful failure. The dialog itself was proven in the first run.
- **The harness, not Casper, had to adapt for the mini.** Sam's turns were
  run inside the mini's logged-in session via a one-shot launchd job,
  because an SSH session can't reach the login Keychain. A person typing in
  Terminal on the mini is in that session already.

**2026-10-01 Hermes + GLM 5.2 run (Nous Portal), on the Mac mini: works,
but consent is not reliable.**
- **Riley:** Hermes, starting from an empty folder via the **URL route**
  ("Set me up with Casper using …/agents.md — my invite code is …").
- **Sam:** set up on the MacBook as fixture, not under test.
- Riley's agent was driven twice:
  1. in one-shot mode (`hermes -z`);
  2. in interactive mode, over ACP: the protocol editors and the desktop
     app use, where Hermes can ask questions.

**What worked:**
- Install, account, pairing, invite, backup and a restore that was
  byte-identical (SHA-256), all through the URL route.
- Hermes masks secrets in its terminal output, so the token
  `setup agent --client other` printed came through redacted. GLM found
  `--json`, then wrote the MCP server into `~/.hermes/config.yaml` itself.
- In interactive mode it asked for the username (with a suggestion) and
  showed the invite preview before redeeming.

**What didn't:**
- **One-shot mode answers every "ask the user" with "pick a default and
  proceed".** That's Hermes' design. GLM chose the username itself and
  redeemed, picked a folder and backed it up, all unasked.
- **Even in interactive mode**, after Riley agreed to *redeem*, it went
  straight on to back up a folder Riley never chose, with no preview and
  no question.
  - It took the folder from a file my harness had left from the one-shot
    run: test contamination, since cleaned up.
  - But it called `backup_push` without a preview, and `backup_push`
    defaulted to `preview=false`, unlike every other sharing tool. Fixed:
    it now defaults to `true`, and its description says to ask which
    folder.
- It **skipped the recovery kit** in both modes.
- When challenged it apologised, explained, and deleted the unwanted
  backup.

**Takeaway:** a less capable or more autonomous agent will treat
preview → confirm as optional. Instructions and defaults narrow that gap
but can't close it. Consent for a person's own consequential actions (their
first backup of a folder, redeeming an invite) needs a check the agent
can't satisfy alone. See the design question in the next round.

## Found and fixed during the runs

- **`setup account create` signed up before checking the Keychain**
  (cross-host run). Over SSH, the Keychain refused the password after the
  account already existed, leaving an account nobody could sign in to.
  Fixed: it now checks the Keychain first and creates nothing if that
  fails. The fix shipped mid-run, so the mini kept the build its agent had
  installed earlier. Account creation succeeded once run inside the
  logged-in session.

- **`setup status` said "Account: none" when a Mac had two accounts.** It
  swallowed the "which account?" error. Riley's agent rightly stopped,
  since that looked like data loss. Fixed: status lists every account and
  says to ask whose it is and pass `--account`.
- **Cloudflare served a stale app build.** It caches `.zip` URLs at its
  edge by default, so after a release `/download/casper-macos.zip` still
  returned the old build byte for byte. Fixed: every onboarding response
  sends `Cache-Control: no-store`, and the published URL is now the
  extensionless `/download/casper/macos`.

## Known issues

- **One MCP connection per macOS user.** `setup agent` registers a single
  `casper` server at Claude Code's user scope, so two Casper accounts on one
  macOS user overwrite each other's agent connection (seen in the run). This
  is fine for the real case of one person per Mac. Two people sharing a
  macOS user would need per-folder (`--scope local`) registration.
- **The agent's first suggestion was the person's whole Documents folder**
  (827 MB). That's within limits and a reasonable default, but a first
  backup might be better suggested small, so the person sees it work
  quickly.
- **An agent may treat one "yes, and do X" as consent to two steps.**
  Riley said "Yes. And let's back up ~/casper-live-test", and the agent
  redeemed the invite and ran the backup without previewing the backup
  separately. The person explicitly asked for both, so this is reasonable,
  but the skill could say that a backup's preview should still be shown.
- **`setup agent` has no Hermes mode.** Agents that mask secrets (Hermes)
  get a redacted token from the plain output; `--client hermes` should
  write Hermes' config directly.
- **Cloudflare blocks Python's default `urllib` user agent** (403) on the
  dev domain; `curl` works. Agents scripting in Python could hit this.
- **`HEAD` on the download returns 405** (GET only). It's harmless for
  agents, but some download tools probe with `HEAD`.
