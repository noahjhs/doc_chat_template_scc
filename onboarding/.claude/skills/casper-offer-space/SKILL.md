---
name: casper-offer-space
description: Offer space on this Mac for a friend's encrypted backups, and invite the friend. Use when the person wants to share backup space with friends, invite someone to back up to them, or check/answer friends' requests.
---

# Offer backup space to friends

You need Casper's MCP tools (`my_casper`, `list_hosts`, `publish_offering`,
`create_invite`, ...). If you don't have them yet, do **casper-setup** first.

## 1. Suggest an offering

Call `list_hosts` to get this Mac's name (role `owner`). Check the free disk
space (`df -h ~`). Then suggest, for example:

> I'd suggest offering **up to 20 GB per friend** on this Mac, with any backup
> that fits in a friend's share stored **without asking you each time**. Your
> friends' files are encrypted before they arrive, so you can never read
> them, and they can't see anything of yours. OK, or would you like a
> different amount, or to approve each backup?

- Keep the suggestion well under the free space.
- Approving each backup (`approve_each_backup=true`) is fine if they prefer
  it. Mention that they'll then be asked each time a friend backs up.

Then:
1. `publish_offering(host=..., max_gb=..., approve_each_backup=..., preview=true)`
2. Show them the plan.
3. Once they say yes, call it again with `preview=false`.

## 2. Invite a friend

Ask who it's for and how much they'd like to give them, suggesting e.g. 10
GB (within the offering's limit). Then:
1. `create_invite(quota_gb=..., for_whom="<their friend's name>", preview=true)`
2. Confirm with them.
3. Call it with `preview=false`.

Give them the **message it returns, exactly**, to send to their friend
however they usually talk (text, email). It contains everything the
friend's agent needs. Tell them:
- the code works once and expires in 7 days;
- they can cancel it with you any time ("cancel the invite").

They can invite more friends the same way.

## 3. Notifications (suggest, don't insist)

If they chose to approve each backup, notifications matter. Suggest Telegram:

```sh
/Applications/CasperGo/Casper.app/Contents/MacOS/Casper setup notifications
```

It opens Telegram; they tap **Start**. If backups are allowed without
asking, notifications are just receipts and optional. Say so.

## 4. Close the loop

Tell them, in a few sentences:
- what they've offered, and that their friend appears once they use the
  invite (they'll be notified, if they linked Telegram);
- that this Mac needs to be **on, awake, and running Casper** for friends to
  back up or restore;
- that they can ask you any time "what have I shared?" (`my_casper`) or
  "stop sharing with <name>" (`revoke`, after you confirm).

## Later: requests from friends

When they ask "anything waiting?", or when `my_casper` says something is:
1. Call `list_approvals`.
2. Read each request to them plainly.
3. Ask what they want.
4. Record exactly that with `decide_approval`.

Never decide for them. Their *own* requests can't be decided by you; they
handle those in Telegram.
