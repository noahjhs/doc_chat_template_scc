---
name: casper-setup
description: Set Casper up on this Mac for the person -- create their account, pair this Mac, and connect this agent to Casper's MCP server. Use when onboarding someone to Casper, or when `Casper setup status` shows a missing step.
---

# Set up Casper on this Mac

Every command here is safe to re-run, and each prints what to do next.

```sh
CASPER=/Applications/CasperGo/Casper.app/Contents/MacOS/Casper
"$CASPER" setup status
```

If Casper isn't installed or running, do step 1 of AGENTS.md first.

## 1. Their account

Ask what name their friends should know them by, suggesting something
short such as their first name in lowercase. It's their username, and
friends see it. Then:

```sh
"$CASPER" setup account create --username <name>
```

- If the name is taken, ask for another.
- If they already have a Casper account (from another Mac), use
  `account login --username <name>` instead. They type their password into
  a macOS dialog; you never see it.

The password is generated and kept in their Keychain. Tell them they never
need to type it.

## 2. Pair this Mac

```sh
"$CASPER" setup pair
```

This connects this Mac to their account and waits until it's online.

- If it times out: make sure the menu-bar ghost is there and not paused,
  and that any first-launch question from macOS has been answered. Then
  re-run it.
- Tell them: "This Mac is now one of your Casper machines. Casper only
  ever does what you (or the friends you choose) are allowed to, and asks
  you about anything else."

## 3. Connect yourself

```sh
"$CASPER" setup agent
```

For Claude Code, this registers Casper as an MCP server (`casper`). For
other agents (`--client other`), it prints the MCP URL and header to
configure; help the person add them.

## 4. One restart

You only load new MCP servers when you start. Tell the person exactly what
to do, and **give them a sentence to say afterwards that carries what you
need**. For example:

> I've connected myself to Casper. Please quit me (type `/exit`), start
> me again in this same folder (`claude`), and then say:
> **"continue setting up Casper — back up, invite code CASPER-ABCD-EFGH-JKLM"**

Adjust the sentence to what they chose: offer space, back up (with their
invite code if they have one), or both.

## After the restart

- Run `"$CASPER" setup status`. It should show the account, this Mac
  connected, and the agent connected.
- Call the `my_casper` tool to confirm you can reach Casper.
- Continue with **casper-offer-space** or **casper-back-up**.
