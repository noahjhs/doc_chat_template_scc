---
name: casper-mirror
description: Keep the person's folders safe by mirroring them, encrypted and with 30 days of history, on friends' computers -- redeem a friend's invite, choose folders, start mirroring (the person confirms), make the recovery kit, and later undo accidents or rebuild a lost Mac. Use when the person wants their files kept safe, has a Casper invite code, wants an old version back, or needs to recover.
---

# Keep the person's files safe (mirroring)

You need Casper's MCP tools. If you don't have them yet, do
**casper-setup** first.

## 1. Get space on a friend's computer

- **They have an invite code** (`CASPER-XXXX-XXXX-XXXX`):
  1. `redeem_invite(code=..., preview=true)`
  2. Tell them what it means.
  3. On their yes, call it with `preview=false`.
- **No code:** call `list_offerings`. If a friend offers mirror space,
  suggest asking for some (`request_access(offering_id, quota_gb)`). If
  they have no friends on Casper yet, explain that a friend can send them an
  invite, and stop here for now.

**Default to generosity.** After receiving space, suggest offering some
back: "Would you like to offer <friend> space on this Mac too? It's not
expected, and they're free to decline." If yes, follow
**casper-offer-space**. If no, that's fine; move on.

`list_hosts` now shows the friend's machine as `<friend>/<machine>` with
role `mirror_peer` (or `catcher_peer`), and this Mac with role `owner`.

## 2. Choose what to mirror

Suggest **Documents** (and **Desktop**). Leave out **Photos** libraries in
this version; they're too large and change constantly. Check the size
(`du -sh ~/Documents`); it has to fit the friend's space.

**A catcher** (optional, but valuable): if the person also has catcher
space from a friend with an always-on machine, include it. If they have no
catcher at all, you may offer Casper's own:

> When your friend's computer is asleep, your newest changes would wait
> on this Mac. Casper can hold just those changes, encrypted, until your
> friend's computer wakes. Want that? (It stops as soon as a friend can
> catch for you.)

If they say yes, pass `use_casper_catcher=true`.

## 3. Start mirroring: the person confirms, not you

1. Call `mirror_folder(source_host=<this Mac>, path="Documents",
   mirrors=["<friend>/<machine>"], catcher="<friend>/<machine>" or "",
   use_casper_catcher=..., preview=true)`.
2. Tell them the plan.
3. On their yes, call it again with `preview=false`.
4. Tell them: **a Casper dialog has opened on this Mac**. They click
   **Allow** (or answer in Telegram). You can't confirm it for them, and
   Casper refuses if you try.

When they say they've allowed it, check `protection_status`. Within a
minute or so it should say "protected -- every change is on at least one
mirror". Large folders take longer the first time.

## 4. The recovery kit (important; do it after mirroring starts)

> If this Mac is lost, rebuilding your folders from your friends' copies
> needs a secret that's only on this Mac. I'll save a copy, locked with a
> passphrase you choose. A box will pop up for you to type it; I won't see
> it.

```sh
/Applications/CasperGo/Casper.app/Contents/MacOS/Casper setup recovery-kit
```

They should keep the saved file (in Documents) **and** the passphrase
somewhere other than this Mac. Make a fresh kit whenever another folder is
mirrored.

## 5. Close the loop

In a few sentences:
- what's mirrored, and where;
- that friends can never read it;
- that it keeps itself up to date from now on, with nothing to do;
- how to undo an accident ("get back yesterday's notes.txt");
- that they'll hear from Casper only if something needs them, e.g. changes
  going unprotected for hours because every mirror is asleep.

## Undoing an accident

1. Call `list_versions(folder="Documents", name_contains="budget")`.
2. Pick the version with them.
3. Call `restore_version(folder, name, at)`, with `at` exactly as listed.

The old version arrives as a new copy under **Casper Restores**; nothing is
overwritten.

## A lost Mac

On the new Mac:
1. Install Casper and sign in to the **same** account
   (`setup account login`).
2. `setup pair`.
3. `setup restore --kit <the recovery kit file>`. The person types the
   passphrase into a dialog.
4. `restore_folder(folder="Documents", dest_host=<the new Mac>)`.
5. Watch `protection_status` until it says restored.

Changes made after the last moment a mirror was in sync come back from
the catcher, if there was one.
