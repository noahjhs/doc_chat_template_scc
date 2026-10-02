# Casper: instructions for the agent

You're helping a person set up **Casper**. Casper lets friends share their
computers safely. In this version, that means **keeping a person's
important folders mirrored on friends' computers**:
- continuously, as files change;
- encrypted, so the friends can never read them;
- with 30 days of history, so accidents can be undone.

It also means **offering space on your own computer** for friends' mirrors.
Once it's set up, it's plumbing: the person shouldn't have to think about
it again.

Casper is in early alpha. It runs on macOS only.

The person has pointed you here and said something like "go" or "set me up".
You do the setup; they answer a few questions and confirm what you propose.
Do it all yourself unless a step below says the person must.

**Casper's community defaults to generosity.** People offer space without
asking anything back. When someone receives space, Casper suggests they
offer some in return, but it's never a condition.

## How to work with the person

- **Suggest, don't presume.** Ask few questions, and give each one a
  suggested answer ("I'd suggest backing up your Documents folder, which is
  1.2 GB. OK?").
- **Preview, confirm, act.** Every Casper tool that shares anything has a
  `preview` option. Call it first, show the person the plan in your own
  words (keep the facts exact), and act only once they say yes.
- **Never handle their secrets.** Casper generates their account password
  and keeps it in the macOS Keychain. Passphrases are typed by the person
  into a macOS dialog you can't see. Never ask them to tell you either.
- **You can't approve your person's own requests,** and Casper won't let
  you. You *can* record their decision on requests from *other* people,
  once they've told you what they want.
- **Teach as you go,** in a sentence or two at a time. By the end, they
  should understand friends, offerings, encryption, approvals, and how to
  undo anything.
- If a command fails, read its message. It says what to do next.

## Step 1: get Casper running on this Mac

This is a Mac-only alpha, so if this isn't macOS, tell the person and stop.

```sh
CASPER=/Applications/CasperGo/Casper.app/Contents/MacOS/Casper
"$CASPER" setup status 2>/dev/null || echo "not installed"
```

If it isn't installed, install it. This downloads the notarized app from
Casper and opens it; there's no installer:

```sh
curl -fsSL {{CASPER_URL}}/download/casper/macos -o /tmp/casper-macos.zip \
  && unzip -oq /tmp/casper-macos.zip -d /Applications \
  && open /Applications/CasperGo/Casper.app
```

Tell the person that a small ghost appears in their menu bar. If macOS asks
whether to open Casper at login, suggest **yes**: friends' backups only
work while Casper is running.

From here on, `"$CASPER" setup status` always tells you what's done and
the **next step**. Re-run it whenever you're unsure.

## Step 2: find out what they want

If they gave you an **invite code** (it looks like `CASPER-XXXX-XXXX-XXXX`),
a friend has already offered them space: they want their folders kept safe.
Keep the code for later.

Otherwise, ask:

> Would you like to **keep your files safe on a friend's computer**, **offer
> space on this computer for friends' files**, or **both**?

## Step 3: set up the account, this Mac, and you

Follow the **casper-setup** skill:
- a folder copy is at `.claude/skills/casper-setup/SKILL.md`;
- or fetch `{{CASPER_URL}}/onboarding/skills/casper-setup.md`.

In short: create their account, pair this Mac, connect yourself to Casper's
MCP server, and have the person restart you once.

## Step 4: do what they came for

After the restart, run `"$CASPER" setup status` again. Then follow:
- to **offer space**: the **casper-offer-space** skill
  (`{{CASPER_URL}}/onboarding/skills/casper-offer-space.md`);
- to **keep their files safe**: the **casper-mirror** skill
  (`{{CASPER_URL}}/onboarding/skills/casper-mirror.md`).

If they want both, offer space first, then mirror.

## Later, any time

The person may ask you things like:
- "What have I shared?" or "Are my files safe?" Use `my_casper` and
  `protection_status`.
- "Get back yesterday's version of budget.xlsx." Use `list_versions`, then
  `restore_version`.
- "Keep my Desktop safe too." Use **casper-mirror**.
- "Anything waiting for me?" Use `list_approvals`, then ask them.
- "Stop sharing with Sam." Use `revoke`, after confirming.
