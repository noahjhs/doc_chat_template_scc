---
name: casper-back-up
description: Back up a folder from this Mac to a friend's computer with Casper -- redeem a friend's invite, choose what to back up, run the encrypted backup, and make a recovery kit. Use when the person wants to back up to a friend, has a Casper invite code, or wants to restore a backup.
---

# Back up to a friend

You need Casper's MCP tools. If you don't have them yet, do
**casper-setup** first.

## 1. Get space on a friend's computer

- **They have an invite code** (`CASPER-XXXX-XXXX-XXXX`):
  1. `redeem_invite(code=..., preview=true)`
  2. Tell them what it means.
  3. On their yes, call `redeem_invite(code=..., preview=false)`.
- **No code:** call `list_offerings`.
  - If a friend offers space, suggest asking for some
    (`request_access(offering_id, quota_gb)`); the friend decides.
  - If they have no friends on Casper yet, explain that a friend who uses
    Casper can send them an invite, and stop here for now.

Call `list_hosts`. The friend's machine appears as `<friend>/<machine>`
with role `backup_peer`, and this Mac appears with role `owner`.

## 2. Choose what to back up

Suggest a folder: their `~/Documents` is a good default. Check its size
(`du -sh ~/Documents`).
- Each backup can be **up to 2 GB** in this alpha, and must fit in their
  share on the friend's machine.
- If it's too big, suggest a smaller, important folder (e.g. a specific
  project or the tax folder), and say why.

Then:
1. `backup_push(source_host=<this Mac>, path=<folder>, dest_host=<friend>/<machine>, preview=true)`
2. Tell them the plan.
3. On their yes, call it with `preview=false`.

## 3. Wait for it, kindly

Check `backup_status` every 20–30 seconds until it's `complete`, `denied`
or `failed` (a few seconds to a few minutes, depending on size).
- If their friend approves each backup, it waits for the friend. Tell them
  so, and that they'll be notified. They don't need to wait with you.
- If it fails, read them the reason; most mean "the friend's computer is
  off or asleep". Suggest trying again later.

## 4. The recovery kit (important; do it once)

Explain:

> The key that decrypts your backups is stored only on this Mac. If this
> Mac is lost, you need a copy of it to restore anything. I'll save one,
> locked with a passphrase you choose. A box will pop up for you to type it;
> I won't see it.

```sh
/Applications/CasperGo/Casper.app/Contents/MacOS/Casper setup recovery-kit
```

Then ask them to keep the saved file (in Documents) **and** the passphrase
somewhere other than this Mac, such as a password manager or a printout.

## 5. Close the loop

In a few sentences:
- what was backed up and where;
- that their friend can never read it;
- how to restore ("restore my Documents backup", using `backup_restore`),
  which puts the files in a new **Casper Restores** folder and never
  overwrites anything;
- that they can ask "what have I shared?" (`my_casper`) or stop any time
  (`backup_delete`, `revoke`).

Offer to back up another folder.

## Restoring

`backup_list` shows their backups. Call
`backup_restore(backup_id, dest_host=<this Mac>)`, then check
`backup_status(backup_id)` until the restore finishes, and tell them where
the files are.

Restoring needs this Mac's key. On a different Mac, they'd need their
recovery kit, and import isn't built yet. Say so honestly if it comes up.
