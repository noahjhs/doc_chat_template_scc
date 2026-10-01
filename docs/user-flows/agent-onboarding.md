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

## Found and fixed during the run

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
- **Not yet run across two machines.** Claude Code needs installing on the
  mini, with one login by you. The script for Sam's side is the same.
- **`HEAD` on the download returns 405** (GET only). It's harmless for
  agents, but some download tools probe with `HEAD`.
