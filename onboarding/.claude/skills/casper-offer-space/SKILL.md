---
name: casper-offer-space
description: Offer space on this Mac for friends' mirrored folders (or, on an always-on machine, as a catcher), and invite a friend. Use when the person wants to share space with friends, invite someone, or answer friends' requests.
---

# Offer space to friends

You need Casper's MCP tools (`my_casper`, `list_hosts`, `publish_offering`,
`create_invite`, ...). If you don't have them yet, do **casper-setup** first.

Casper defaults to generosity: an offering asks nothing in return. Say so;
it's part of what people are known for here.

## 1. Suggest an offering

Call `list_hosts` to get this Mac's name (role `owner`). Check the free disk
space (`df -h ~`). Then suggest, for example:

> I'd suggest offering **mirror space: up to 20 GB per friend** on this
> Mac. Friends' folders arrive encrypted, so you can never read them, and
> they can't see anything of yours. They get a continuous copy with 30 days
> of history. Nothing is asked in return. OK, or a different amount?

- Keep the suggestion well under the free space.
- **If this Mac is always on** (a desktop or home server that never
  sleeps), also suggest **catcher space**. When a friend's mirrors are
  asleep, their newest changes wait here, encrypted, usually only megabytes,
  until a mirror wakes. For their friends, this is the most valuable thing
  an always-on machine can give.

Then:
1. `publish_offering(host=..., max_gb=..., kind="mirror" or "catcher", preview=true)`
2. Show them the plan.
3. Once they say yes, call it again with `preview=false`.

## 2. Invite a friend

Ask who it's for and how much, suggesting e.g. 10 GB. Then:
1. `create_invite(quota_gb=..., for_whom="<their friend's name>", preview=true)`
2. Confirm with them.
3. Call it with `preview=false`.

Give them **the message it returns, exactly**, to send to the friend however
they usually talk. It contains everything the friend's agent needs. The code
works once and expires in 7 days, and you can cancel it ("cancel the
invite").

## 3. Close the loop

Tell them, in a few sentences:
- what they've offered, and that they'll be notified when the friend joins;
- that this Mac needs to be **on, awake, and running Casper** for friends'
  changes to arrive. While it's asleep, friends' changes wait (or go to a
  catcher), so nothing is lost;
- that they can ask "what have I shared?" (`my_casper`) or "stop sharing
  with <name>" (`revoke`, after you confirm). Stopping leaves friends 7 days
  to recover their copy before it's deleted.

## Later: requests from friends

When they ask "anything waiting?":
1. Call `list_approvals`.
2. Read each request to them plainly.
3. Ask what they want.
4. Record exactly that with `decide_approval`.

Never decide for them. Their *own* confirmations can't be given by you.
