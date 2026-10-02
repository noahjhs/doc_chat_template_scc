# Design: mirroring with history (v1)

**Status:** draft, 2026-10-01; open questions resolved by the Principal the same day. Replaces peer backup as the v1 product. The
trust framework, invites, ledger, agent onboarding, notifications and
recovery kit all carry over. The engine is Syncthing
([spikes/syncthing-mirroring.md](../spikes/syncthing-mirroring.md)).

## The product in one paragraph

Your important folders are kept, continuously and encrypted, on your
friends' computers, with 30 days of history. You set it up once, by asking
your agent ("keep my Documents safe with Sam and Mira"), and confirm it once.
After that it's plumbing: changes reach a friend within seconds, and an
accident (a deleted folder, a bad edit, ransomware) can be undone from
history. A lost laptop is rebuilt from the mirrors with your recovery kit.
Nobody, including your friends and Casper, can read your files.

## What people experience

**Setup:** one conversation, one confirmation.
1. The agent suggests what to mirror, with sensible defaults (Documents,
   Desktop), and who: friends who offer mirror space, or invite someone new.
2. The person confirms **outside the agent**: a native Casper dialog on
   their Mac, or Telegram. This is the Hermes/GLM lesson: a person's own
   consequential actions need a check the agent can't satisfy alone. It
   happens once per mirror arrangement, not per file.

**After that:** nothing, unless something needs them. Casper tracks
**protection status** and only nudges when it degrades:

> *Your Documents are protected — last change mirrored 40 seconds ago
> (Sam: up to date; Mira: asleep since 22:10, catching up when she's back).*

> *Nudge:* "12 changes from today aren't on any mirror yet — Sam's and
> Mira's Macs have both been off since this morning."

**Undo:** "get back yesterday's version of budget.xlsx" or "restore the
folder I deleted", through the agent. Restores go into a new folder and
never overwrite anything.

**Disaster:** a new Mac, the Casper app, the recovery kit; everything comes
back from the mirrors.

## Roles

| Role | Holds | Typical machine |
|---|---|---|
| **Owner** | The real files | Your laptop or desktop |
| **Mirror** | An encrypted, versioned copy of the whole folder | A friend's computer, possibly a laptop that sleeps |
| **Catcher** (fallback) | Encrypted copies of **only the changes no mirror has yet** | An always-on machine: a friend's or group's home server, Mac mini or NAS |

## Sleeping mirrors and fallbacks

**The problem.** A mirror that sleeps leaves a window in which your latest
changes exist only on your machine. The risk is the overlap: changes made
while every mirror is asleep, and then your machine is lost before any
mirror wakes up. That window is what disaster-recovery planning calls the
**recovery point objective (RPO)**: how much recent work you could lose. We
want it small, measured, and visible.

### Layer 1: more than one mirror

A second mirror is just a second encrypted device on the same folder, and
the spike showed it's trivial with Syncthing. Two friends who sleep at
different times cut the window a lot. Casper knows each machine's presence
history (it already receives presence reports), so it can **recommend
mirrors by observed availability** and avoid pairing two machines that
sleep at the same time.

### Layer 2: a catcher, holding only the gap

**A fallback doesn't need the whole mirror.** It only needs to cover what
*no* mirror has yet:

- **What's at risk:** files whose current content isn't yet on any mirror.
  Syncthing reports this per device (`/rest/db/remoteneed`). The set at
  risk is what *every* mirror still needs.
- **What isn't:**
  - Deletions. The data still exists on the mirrors, so in the worst case
    a deleted file reappears after a disaster, which is acceptable.
  - Files the mirrors already have.

Mechanism, inside the owner's Casper daemon:
1. Watch which files are at risk, using Syncthing's per-device need lists.
2. Copy the at-risk files into a small **staging folder** on the owner's
   Mac. Hard links are used where the file system allows, so in most cases
   they take no space.
3. Share the staging folder, not the real one, with the catcher, as an
   ordinary Syncthing **encrypted** folder. The catcher holds only those
   files, encrypted, and can't read them.
4. Once any mirror has caught up on a file, remove it from staging.
   Syncthing propagates the removal and the catcher's copy goes away. The
   catcher keeps no history, so its storage is just the current gap.

The catcher's storage is bounded by *how much changes while every mirror is
asleep*: usually megabytes, not the whole library. Casper enforces a
quota; if the gap outgrows it, the person is nudged.

**Restoring after a disaster** overlays the catcher on the mirror. Rebuild
from the freshest mirror, then apply the catcher's files, which are newer
by construction. Casper's restore does this; the person sees one action.

### Who runs catchers

The design point is that **always-on capacity is a community resource**.
Someone in the circle with a home server, NAS or Mac mini catches for
several friends at once, using little storage. That's goal 3 (resilience
through shared infrastructure) as a concrete role. In the trust framework
it's another offering, **catcher space**, small and always-on, granted
like mirror space.

**Casper as the catcher of last resort** (decided 2026-10-01). Casper's
server offers to be the catcher **precisely when a person has no other
catcher**. The agent suggests it, the person accepts or declines, and it
holds only ciphertext. As soon as a friend's always-on catcher exists,
Casper's role ends and its copy is dropped.

This sits within the sovereignty goal because of a later stage the
Principal has in mind: **community-hosted instances of the Casper service
itself**. The "Casper" catching for you would then be your community's own
instance, not a single vendor.

### Protection status, precisely

For each mirrored folder, Casper computes:
- **Unprotected changes:** how many files, and how old the oldest, are on no
  mirror and no catcher. This is the honest number.
- **Last fully mirrored:** when every mirror was last fully in sync.

The ledger (`my_casper`) shows these. A nudge fires only when unprotected
changes are older than a threshold (say 6 hours) **and** nothing is about
to fix it.

## How it maps onto Syncthing

| Casper concept | Syncthing mechanics, all automated |
|---|---|
| Pairing a Mac | The daemon starts its bundled Syncthing with its own home and registers the device ID with `casper_service` |
| Invite / grant of mirror space | `casper_service` introduces the two device IDs to each other's daemons. Nobody ever sees a device ID. |
| Mirroring a folder to a friend | The owner's folder is shared with the friend's device **with a folder password**. The friend's daemon creates a `receiveencrypted` folder with versioning (see History below). |
| Folder password | Generated per folder, kept in the Keychain, and protected by the recovery kit (it replaces the age key for mirroring) |
| Catcher | The staging folder, shared encrypted with the catcher's device, without versioning |
| Undo / restore a version | A Casper action: decrypt the needed `.stversions` entries (stripping the `~timestamp` suffix, the gap the spike found) into a new folder |
| Lost machine | A new device plus the recovery kit (folder passwords); Syncthing pulls from the mirrors, then the catcher overlay |
| Revoke | Unshare. After the 7-day grace period, the friend's daemon removes its encrypted copy. |
| Quota | Checked by Casper before sharing, then monitored. Syncthing has no quotas. |
| Finding peers | Casper-run discovery (`stdiscosrv`) and relay (`strelaysrv`); never Syncthing's public servers |

The trust and enforcement rule still holds: **each daemon decides what it
accepts from its own grants.** A friend's daemon only accepts devices and
folders that a grant on its own host allows, re-checked from
`casper_service`, never just because a peer offered them.

## MCP tools (proposed)

- `mirror_folder(path, mirrors[], preview)`: preview, then out-of-agent
  confirmation.
- `protection_status()`: per folder, the numbers above, in plain words.
- `list_versions(path)`, `restore_version(path, when)`,
  `restore_deleted(path)`: always into a new folder.
- `offer_space(kind = mirror | catcher, ...)`: extends `publish_offering`.
- The existing ledger, invites, revoke and approvals carry over.

## Retired from the peer-backup v1

The snapshot backup actions (`backup_*`) and the orchestration that passed
ciphertext through `casper_service`. Keep them until mirroring ships, then
remove them. The integration test's approach (real daemons, real service)
carries over to mirroring.

## Build phases (for when this is approved)

1. **Bundle and run Syncthing** in the daemon; register device IDs; set up
   self-hosted discovery and relay on the mini.
2. **Mirroring a folder** to one friend, end to end through invites, with
   out-of-agent confirmation (native dialog plus Telegram).
3. **Protection status and the ledger;** a second mirror.
4. **Catchers:** staging, the catcher offering, and overlay restore.
5. **Undo and disaster restore** through the agent; then the onboarding
   skills rewritten for mirroring, and live runs (Claude and Hermes).

## Decisions (2026-10-01)

1. **Casper is the catcher of last resort,** offered only when someone has
   no other catcher (see above).
2. **History: every version for 7 days, then one per day up to 30 days.**
   It's easy to explain: "anything from the last week, and a daily
   snapshot for the month before". Syncthing's built-in staggered
   versioning only approximates this (every 30 s for the first hour,
   hourly for a day, daily for 30 days). Since version files carry
   timestamps in their names, the **mirror's own daemon prunes
   `.stversions` to the exact policy**, and Syncthing keeps everything in
   between.
3. **Photos are out of v1.** The default folders are Documents and Desktop.
   The agent can add other folders on request, within the size limits.
4. **Default to generosity, not reciprocity** (the community model; see
   [values-and-goals.md](../values-and-goals.md)).
   - **Publishing** a mirror or catcher offering defaults to **no
     reciprocity required**. You give space; nothing is asked back.
   - **Looking for** mirroring defaults to **offering reciprocity**: when
     you ask a friend to mirror for you, Casper's suggestion includes
     mirroring for them in return, which they can decline.
   - Reciprocity and barter stay possible, but they aren't the featured
     model.
   - People can be **known as resource providers** to their community. The
     ledger and friends' views show what someone gives, not what they owe.
5. **Discovery and relay servers run on the mini** for the alpha. This is
   another reason it must come back by itself after a restart.

## Still open

- How "known as a resource provider" shows up: on a friend's profile, in
  the ledger, in invites ("Sam keeps copies for 4 friends")? This is a
  design pass of its own, and it should come after the landing-page work.
