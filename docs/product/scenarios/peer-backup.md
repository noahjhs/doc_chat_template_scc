# Scenario: peer backup (v1)

**Status:** built and passed live on dev 2026-09-28 (see
[docs/user-flows/peer-backup.md](../../user-flows/peer-backup.md)). This models the
workflow; it is not a test flow (those live in `docs/user-flows/`). Once
built, it gets a matching user flow there.

Riley backs up a folder from their laptop to their friend Sam's Mac mini,
using their own agent (e.g. Claude) connected to Casper over MCP. Sam's
machine stores the backup but can never read it. Riley can restore it
later, including after losing their laptop.

## Cast

| Who | What |
|---|---|
| Riley | Backup owner. Has host `riley-laptop` paired. |
| Sam | Backup peer and host owner. Has host `sam-mini` paired. |
| Riley's agent | Any MCP client, acting on Riley's behalf. |
| Casper | The app server: MCP server, trust framework, approvals, notifications. Handles ciphertext only. |

## Preconditions

- Riley and Sam each have an account and a paired, connected daemon.
- Both have linked a notification channel (Telegram in v1). Sam needs one
  to approve; Riley needs one to hear the outcome.

## Steps

### 1. Riley and Sam become friends

Riley sends Sam a friend request and Sam accepts it.
→ tuples `(riley, friend, sam)` and `(sam, friend, riley)`.

*Why this step exists:* backup peering requires an existing relationship
(risk model, point 5). It's the first place the trust framework appears in
the product.

### 2. Sam publishes an offering

Sam offers **Backup space** on `sam-mini`, visible to friends, with up to
20 GB per peer. The offering uses the **Backup Peer** role, which is
app-layer curated and not editable in v1. A holder of the role can:

- **write** opaque blobs into their own quota'd inbox on `sam-mini`. The
  tier is Sam's choice. In practice this would usually be **allow**, set
  once. *This scenario uses **ask** deliberately, because it's the v1 test
  of third-party approval.*
- **allow:** list and delete *their own* blobs.
- **deny:** everything else. That includes running commands, reading
  outside the inbox, and reading other peers' blobs.

There are no duties attached, so a request only needs Sam's approval, not
an acceptance step.

### 3. Riley requests, Sam grants

Riley looks at what Sam offers, sees Backup space, and requests it with a
10 GB quota. Sam is notified and approves.
→ tuple `(riley, backup-peer, sam-mini)`, with a 10 GB quota.

Sam can see who holds the role, each holder's quota, and their current
usage. Sam never sees file names or contents.

### 4. Riley connects their agent

Riley creates an agent token (`harness agent token create claude`) and
adds Casper as an MCP server in their agent with it (Claude Code accepts a
bearer header; OAuth for other clients comes after v1).
→ the agent acts **on behalf of** Riley, with permissions at most Riley's.
In v1, grants apply to the Riley-and-agent pair as one unit.

### 5. Riley asks for a backup

> "Back up `~/Documents/taxes` to Sam's machine."

The agent calls `list_hosts` and sees `riley-laptop` (owner) and `sam-mini`
(Backup Peer, 10 GB quota, 0 used). It then calls:

```
backup_push(source_host="riley-laptop", path="~/Documents/taxes", dest_host="sam-mini")
```

**If either host is offline, the call fails fast** with a plain error, e.g.
"sam-mini is offline". It is not queued.

### 6. Casper checks both sides

- **Source:** reading the folder on `riley-laptop` is checked against
  Riley's *own* policy by Riley's daemon. Riley is the owner, so it's
  normally allowed.
- **Destination:** the write into Riley's inbox on `sam-mini` is checked by
  Sam's daemon under the Backup Peer layers. It's ask-tier and not yet
  approved, so **nothing is transferred yet.**

### 7. Sam is asked; Riley will be told

- Sam gets a notification: *"Riley wants to store 1.2 GB of encrypted
  backup on sam-mini (quota 10 GB). Approve / Deny."* There are no file
  names, because Sam can't see them.
- The MCP call returns immediately. The agent tells Riley: *"Waiting for Sam
  to approve. You'll be notified when it's done."*
- Riley doesn't poll. **Casper notifies Riley directly** once the backup
  completes, is denied, or fails.
- Later, this pause can also be expressed as an MCP Tasks `input_required`
  status for clients that support Tasks.

### 8. Sam approves; the backup runs

Casper re-sends the whole operation to both daemons with the approval
marker. Neither daemon reuses its earlier verdict; both re-match the policy
from scratch, as dispatch already works today. If a host has gone offline
since step 5, the operation fails fast and Riley is notified.

1. `riley-laptop` reads the folder, **encrypts it with Riley's key**
   (contents, names and structure), signs the blobs, and streams the
   ciphertext.
2. The ciphertext passes through relay and Casper, which never hold a key.
3. `sam-mini` writes the blobs into Riley's inbox, enforcing the quota.

Riley gets a notification: *"Backed up taxes to sam-mini: 1.2 GB."*

### 9. Restore

Riley asks their agent to restore `taxes` from Sam's machine to
`riley-laptop`. Reading back *their own* blobs is authorized by blob
ownership, not by the Backup Peer role. Decryption happens only on a host
that holds Riley's key.

### 10. Revocation

Either side can end the arrangement at any time.

- **Sam revokes the role:** Riley is notified, and their blobs are deleted
  after a grace period long enough for Riley to move them elsewhere.
- **Riley deletes the backup:** the blobs are removed and Sam's usage view
  updates.

## Keys (v1)

Riley's daemon generates the backup key and stores it in the **macOS
Keychain as a synchronizable item**. iCloud Keychain copies it, end-to-end
encrypted, to Riley's other Apple devices, so losing the laptop doesn't
lose the key. A passphrase-wrapped key export is the fallback for people
without iCloud Keychain.

This makes recovery depend on Apple, which is in tension with goal 2. That
is accepted for the alpha. The long-term answer is splitting the key among
trusted friends (Shamir secret sharing), so the key format should allow for
it from the start.

## What v1 proves

- agent → MCP → Casper → daemon works end to end, on two hosts.
- The request → grant flow, gated by a relation, decides what a non-owner
  can do on someone else's machine.
- A third party (the host owner) is notified and approves, the operation
  completes, and the requester is notified of the outcome.
- The backup host holds only ciphertext it cannot read.

## Out of scope for v1

- Replication to several peers (goal 3), incremental or deduplicated
  backups, and scheduled backups.
- Delegating approvals to a person or an agent. This is the eventual happy
  path (see [trust-framework.md](../trust-framework.md)), but v1 has one
  approver: the host owner.
- Offers with duties, and conditions on offerings (e.g. "declare what's in
  the backup").
- Granting access to the principal separately from their agent.
- A tool for the agent to notify its own principal. Casper's own
  notification to Riley covers v1.
- Content scanning (see [risk-model.md](../risk-model.md)).
- Queuing and retry when a host is offline.

## Deferred questions (after v1)

- **Pass-through vs. direct transfer.** v1 streams ciphertext through relay
  and Casper. That's simple, but uses bandwidth and puts Casper in the data
  path (as ciphertext only). Direct daemon-to-daemon transfer is a later
  option.
- **Key recovery beyond Apple.** Splitting the key among friends, and
  support for other platforms.
- **Where the encryption action lives in the daemon.** This is a design
  task for implementation, touching both `agent/` and the MCP tool surface.
