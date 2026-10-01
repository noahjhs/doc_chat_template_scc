# Scenario: agents onboard people (v1)

**Status:** built, and passed live on dev 2026-09-30 ([run record](../../user-flows/agent-onboarding.md)). Applies [agent-first-ux.md](../agent-first-ux.md).
It replaces the `harness` steps in [peer-backup.md](peer-backup.md) with
something a person can actually do: point their agent at Casper and say
"go".

## The bar

A person sets up either side of peer backup by having their agent do it.
Their own part is limited to:

- pointing their agent at Casper ("go");
- answering a few questions, each with a suggested answer;
- confirming plain-language plans;
- typing a recovery passphrase into a macOS dialog the agent never sees;
- restarting their agent once.

## Getting started

Either of these works:

- **A URL**: "Set me up with Casper: https://<casper>/agents.md". The
  agent fetches the instructions; no files needed.
- **A folder**: download and unzip `https://<casper>/onboarding.zip`, open
  the agent there (`cd casper && claude`), and say "go". The folder holds
  `AGENTS.md` (read by Codex and most agents), a `CLAUDE.md` that imports
  it, and Claude Code skills under `.claude/skills/`. The skills keep
  working after setup ("back up my Photos to Sam").

Both are served by `casper_service` from the repo's `onboarding/`
directory, with the deployment's real domain filled in.

## Why invite codes

Mutual friendship needs each person to know the other's username, and in
practice one friend sets up before the other has an account at all. So
borrow Tailscale's sharing invites:

- Sam's agent creates a **single-use invite** for a specific amount of
  space. It expires after 7 days and can be cancelled.
- Sam sends it to Riley however they normally talk. The agent writes the
  message, which includes the URL and the code.
- Riley's agent **redeems** it. That makes Riley and Sam friends and gives
  Riley the space, in one step.

Consent is still two-sided:
- Sam consented when creating the invite for that amount;
- Riley consents by redeeming it, after seeing a plain-language preview.

Sam is notified when it's used. Adding friends by username stays available
for people who already know each other's usernames.

## Mechanics: `casper setup` (built into the Casper app)

`/Applications/CasperGo/Casper.app/Contents/MacOS/Casper setup <command>`.
Every command is safe to re-run, prints plain text by default or JSON with
`--json`, and reports what to do next.

| Command | Does |
|---|---|
| `status` | Reports what's done and the next step: installed and running, account, this machine paired and connected, agent connected, notifications, recovery kit made |
| `account create --username U` | Signs up with a generated password. The password and session are kept in the Keychain; the person never handles them |
| `account login --username U` | For an existing account on a second machine. Reads the password from a macOS dialog |
| `pair` | Pairs this Mac to the account and waits until it shows as connected |
| `agent [--client claude-code]` | Creates an agent token and registers Casper as an MCP server for the agent, then says to restart the agent. For other clients, it prints what to configure |
| `notifications` | Opens the Telegram link, for nudges and approvals outside the agent (optional) |
| `recovery-kit` | Asks for a passphrase **in a macOS dialog** and saves this machine's backup keys, encrypted, to `~/Documents/Casper Recovery Kit (<user>).txt`. The agent never sees the passphrase |

Installing is the one plain-shell step: `curl` the zip from
`https://<casper>/download/casper/macos`, unzip it into `/Applications`,
and open it once. A command-line download isn't quarantined, so there's no
"downloaded from the internet" prompt. The first launch asks once about
login items, which is the person's choice.

## MCP tools added for onboarding

| Tool | Notes |
|---|---|
| `my_casper` | **The ledger:** friends, offerings, invites, space given and held, backups, and anything waiting for a decision, in plain words |
| `publish_offering(host, max_gb, approve_each_backup, preview)` | Offer backup space to friends |
| `create_invite(offering_id, quota_gb, preview)` | Returns the code and a ready-to-send message |
| `redeem_invite(code, preview)` | Becomes friends with the inviter and receives the space |
| `add_friend(username)` | For people who already know each other's usernames |
| `list_approvals` / `decide_approval(id, approve)` | Only requests **from other people** (a friend's request or a friend's backup). The person's own requests can't be decided here (Rule of Two) |
| `revoke(kind, id)` | Undo: space given or held, an invite, an offering |
| `backup_push(…, preview)` | A preview states size, destination, space left, and "they can never read it" |

`preview=true` returns the plain-language plan without changing anything.
The instructions require: preview → show the person → their yes → do it.

## The two flows

**Sam, offering space ("go"):**
1. Check `casper setup status`. Install if needed.
2. Ask what they want: offer space, back up, or both.
3. Ask for a username, suggesting one. `account create`.
4. `pair`, then `agent`. Tell Sam to restart the agent and say "continue".
5. *(After restart.)* Suggest an offering: "Offer up to 20 GB on this Mac to
   friends; any backup within their share is allowed without asking you."
   Preview, then confirm.
6. Ask who it's for and how much. Create an invite (preview, confirm), and
   hand Sam the message to send.
7. Recommend Telegram, which is optional when backups are allowed without
   asking.
8. Teach: what Sam's friends can and can't do, how to see it all
   ("what have I shared?"), and how to undo it. Point out that Casper must
   be running for friends to use the space.

**Riley, backing up ("set me up … my invite code is X"):**
1–4. Same as Sam.
5. Redeem the invite: preview ("you and Sam become friends; you get 10 GB on
   Sam's Mac; Sam can never read it"), confirm.
6. Ask what to back up, suggesting a folder. Preview the backup, confirm,
   push.
7. **Recovery kit.** Explain that the keys live only on this Mac, run
   `recovery-kit`, and tell Riley to keep the file and the passphrase
   somewhere other than this Mac.
8. Teach: how restoring works, the ledger, and how to undo.

## Live-run plan

Fresh `claude -p` sessions on this laptop, given only the URL or the
folder, play each person. Both people's accounts are paired to the same Mac:
the daemon supports several accounts on one machine, and cross-machine
transfer was already proven 2026-09-28. Claude Code isn't installed on the
mini. Once it is, the run should be repeated across both machines.

The agent's questions are answered with short, human-sized replies. The run
passes when both flows finish, the backup completes and restores, and
nothing in the run needed a human to type anything beyond those replies,
the passphrase dialog, and the restart.

## Out of scope for this round

- MCP Apps approval cards (needs OAuth).
- Windows/Linux.
- Choosing a different folder to back up later via a schedule.
