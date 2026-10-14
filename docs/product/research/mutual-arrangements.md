# Mutual arrangements: prior art

Research for a design conversation, written 2026-10-14. The Principal wants:

- a **standardized mutual arrangement** (two people join each other's
  mirror group), with a catchy name, as the core value proposition;
- room for **vastly asymmetric sharing** (someone with no spare space
  drawing on someone with lots): the continuum between barter and charity
  is a key problem space;
- **low-friction spreading** of copies, wider, with little or no
  intervention;
- **graph semantics** that serve this app and later ones (sharing other
  resources).

This is what exists, what worked, and what didn't. Principles and name
ideas are at the end. Related: [values-and-goals.md](../values-and-goals.md)
("default to generosity"), [trust-framework.md](../trust-framework.md),
[scenarios/mirroring.md](../scenarios/mirroring.md).

## 1. Friend-to-friend and peer storage

| System | Model | What happened |
|---|---|---|
| **CrashPlan** "back up to a friend" (Code42) | Free P2P backup to a friend's computer via an invite code; the paid cloud was the business | Killed with the whole consumer product in Oct 2018 as Code42 chased business customers ([TidBITS](https://tidbits.com/2017/08/22/crashplan-discontinues-consumer-backups/)). The friend feature was loved but unmonetized, so it went with the platform. |
| **Wuala** (LaCie) | **Storage trading**: give local disk, earn cloud space = space × fraction of time online, min. 4 h/day ([SmallNetBuilder](https://www.smallnetbuilder.com/cloud/storage/wuala-by-lacie-reviewed/)) | Trading dropped in 2011, officially for networking/security reasons ([StorageNewsletter](https://www.storagenewsletter.com/?p=114021)); service shut in 2015 ([Wikipedia](https://en.wikipedia.org/wiki/Wuala)). Strangers' always-on uptime was the scarce, gamed resource. |
| **Pastiche / Samsara** (Cox & Noble, research) | P2P backup among strangers; Samsara enforces fairness with "claims" (placeholder data you must store in exchange) and probabilistic punishment ([Pastiche](https://static.usenix.org/event/osdi02/tech/full_papers/cox/cox.pdf), [Samsara](https://www.cs.rochester.edu/meetings/sosp2003/papers/p135-cox.pdf)) | Elegant, never mainstream. Most of the machinery exists only because the peers are strangers. |
| **Friendstore** (Tran, Chiang, Li, 2008) | Backup **only to nodes you trust in real life**; capacity budgeted by upload bandwidth, not disk ([dblp](https://dblp.org/search?q=Friendstore)) | Key finding: trusting friends removes most of the anti-cheating complexity; the real limits are bandwidth and uptime. Closest research ancestor of Casper. |
| **Tahoe-LAFS** | Client encrypts and erasure-codes (k of n shares) onto a **grid** of storage servers; "servers of happiness" = how many distinct servers must hold shares so any k of them can rebuild ([spec](https://tahoe-lafs.readthedocs.io/en/latest/specifications/servers-of-happiness.html)) | Still alive, technically excellent; used by small "friendnet" grids. Adoption limited by operational complexity: running a grid is a sysadmin hobby. |
| **Storj, Sia, Filecoin** | Paid markets: hosts earn tokens, proofs of storage, contracts, collateral and slashing | Work as businesses, but incentives are heavy and hard to get right; a decade on, incentive-compatibility is rarely proven ([survey](https://www.arxiv.org/pdf/2510.11866)); [Filecoin](https://en.wikipedia.org/wiki/Filecoin) revenue leans on penalties. Money turns friends into vendors. |
| **IPFS pinning** | Content stays only while someone **pins** it ([docs](https://docs.ipfs.tech/concepts/persistence/)) | Nobody pins strangers' data for free, so paid pinning services grew. Lesson: availability needs an explicit holder with a reason. |
| **Freenet** | Everyone donates a datastore; popular data survives, unrequested data is evicted ([Wikipedia](https://en.wikipedia.org/wiki/Freenet)) | Pure altruism works for publishing, not for keeping *your* files. |
| **Syncthing, Resilio (BitTorrent Sync)** | Device-to-device sync; Syncthing's **untrusted (encrypted) devices** hold data they can't read ([docs](https://docs.syncthing.net/users/untrusted.html)); Resilio went enterprise in 2016 ([Wikipedia](https://en.wikipedia.org/wiki/Resilio_Sync)) | Great engines, no social layer: pairing devices by IDs, no notion of "my friends" or reciprocity. Casper is this layer. |
| **BitTorrent** | Tit-for-tat choking rewards uploaders ([Cohen](https://bittorrent.org/bittorrentecon.pdf)) | Works for short swarms; gamed by strategic clients ([BitTyrant](https://static.usenix.org/events/nsdi07/tech/piatek/piatek_html/bittyrant.html)). Free-riding was the norm in Gnutella: ~70% shared nothing ([Adar & Huberman](https://firstmonday.org/ojs/index.php/fm/article/view/792)). |

**What the pattern says.** Every *stranger* system needed metering
(trading ratios, claims, tokens, proofs) and either failed or became a
market. The *friend* systems failed for business reasons, not social ones:
CrashPlan's friend backup was popular and free, and died as a loss leader.
Strict ledgers didn't stop free-riding among strangers; real-world trust
made ledgers mostly unnecessary among friends. The hard problems among
friends are **uptime, bandwidth, and keeping someone responsible for
each copy**.

## 2. Gift and mutual-aid economics

Four modes along the barter-to-charity continuum:

- **Barter / balanced exchange**: equal value, now, settled. Clean, and
  it ends the relationship once settled.
- **Reciprocity (gift exchange)**: Mauss's *The Gift*: the obligations to
  give, receive, and reciprocate bind people *because* they are never
  quite settled ([Wikipedia](https://en.wikipedia.org/wiki/The_Gift_(essay))).
- **Baseline communism**: Graeber's term for "from each according to
  ability, to each according to need" in everyday life, where "not only
  are no accounts taken, but it would be considered offensive... to even
  consider doing so" (*Debt*, ch. 5;
  [summary](https://frontalobe.substack.com/p/chapter-5-part-1-treatise-on-the)).
  Friends lending a ladder. This is where Casper's "default to generosity"
  lives.
- **Charity**: one-way, often to people you don't know, and the receiver
  can feel diminished.

Mechanisms in between:

- **Mutual credit** ([Wikipedia](https://en.wikipedia.org/wiki/Mutual_credit)):
  balances start at zero and sum to zero; giving earns credit, receiving
  goes negative, within limits. Holochain's HoloFuel and Matthew Slater's
  **Credit Commons** (small cells trading mostly inside, linked through a
  common space) are the P2P versions
  ([Slater](https://matslats.net/decentralised-social-networks-holochain-mutual-credit)).
- **Timebanks** (Edgar Cahn): one hour = one hour, whoever gives it; built
  to value the "core economy" of family and community
  ([SSIR](https://ssir.org/articles/entry/the_time_bank_solution)).
  Equalizes contributions, but people with most to give often stop
  logging.
- **Pay it forward** (upstream or generalized reciprocity: A helps B, B
  helps C). Caution: in experiments, greed was paid forward more reliably
  than generosity, while equal treatment was paid forward in kind
  ([Gray, Ward & Norton 2014](https://dash.harvard.edu/bitstream/1/10578870/1/gray,ward,norton-paying_it_forward.pdf)).
  Defaults that look fair propagate.
- **ValueFlows / REA** (Resource, Event, Agent; McCarthy 1982): a
  vocabulary for economic networks where flows go Intent → Commitment →
  Event. It models "non-reciprocal one-way transfers, such as in a gift
  economy" alongside exchanges, which are "at least two transfers with
  reciprocity", including multilateral cycles
  ([ValueFlows](https://www.valueflo.ws/concepts/exchanges/)). Useful as a
  record shape: an offering is an Intent, a grant a Commitment, a mirror
  holding data an ongoing Event.

**Avoiding free-riding without scorekeeping.** What works in small groups:
- **Visibility of giving, not of owing**: a reputation for providing
  (which our values already choose) motivates without debts.
- **Gratitude and acknowledgement**: thanks to the giver, visible to the
  group.
- **Group boundaries and norms**: Ostrom's design principles for commons
  (clear membership, rules made by those affected, graduated responses,
  cheap conflict resolution) ([Wikipedia](https://en.wikipedia.org/wiki/Elinor_Ostrom)).
- **Soft balance signals**: showing a group is "running low" invites
  giving without naming who owes.

## 3. Relationship and graph models

- **Zanzibar ReBAC** ([paper](https://research.google/pubs/zanzibar-googles-consistent-global-authorization-system/);
  [OpenFGA](https://openfga.dev/), [SpiceDB](https://authzed.com/spicedb)):
  tuples `object#relation@user`, where the user can be a **userset**
  (`group:mutual-aid#member`), plus rewrite rules (`viewer` includes
  `editor`; a doc inherits its folder's viewers). Casper's
  `relation_tuples` already follows this. A group-level offering is one
  tuple aimed at a userset, not N pairwise grants.
- **Group offerings vs pairwise grants.** Pairwise is legible ("Sam holds
  my Documents") but scales as N². A group pool ("my 200 GB is for Mutual
  Aid") lets placement decide who holds what, and membership changes don't
  need new grants. A mix fits best: pool by default, pinned pairs when
  someone wants them.
- **Placement.** **Rendezvous (highest random weight) hashing** lets
  everyone independently compute the same top-k holders for a folder, and
  only that folder's data moves when a member joins or leaves
  ([Wikipedia](https://en.wikipedia.org/wiki/Rendezvous_hashing)). Ceph's
  **CRUSH** places replicas across **failure domains** (host, rack, site)
  ([docs](https://docs.ceph.com/en/latest/rados/operations/crush-map/));
  for us, domains are household, network and city. Tahoe's "servers of
  happiness" is the right health measure: distinct holders, not copies.
- **Social recovery guardians.** Buterin's case for wallets recovered by a
  majority of guardians; guardians should not all know each other, and
  should be spread across places and devices
  ([essay](https://vitalik.eth.limo/general/2021/01/11/recovery.html)).
  Argent adds a 36-hour cancellable delay; Apple has Recovery Contacts who
  only provide a code ([Apple](https://support.apple.com/en-us/102641)).
  See [recovery.md](../recovery.md).

## 4. Low-friction spreading

- **"Hold for the group" offerings.** A person offers space to a group,
  not a person; any member's folder can land there, chosen by placement.
  CrashPlan needed one invite per friend; Tahoe needed a grid admin. A
  group pool needs neither.
- **Replication targets, not destinations.** The owner states intent ("2
  copies, on different networks; at least one always on") and the system
  keeps it true, like Ceph rules or Tahoe's happiness. When a holder
  leaves or sleeps for days, Casper picks a replacement by itself.
- **Nudges at the right moment.** Only when protection degrades ("one
  copy left"), and with a one-tap remedy ("Ask Mutual Aid for one more
  holder?"). Consent stays per arrangement, not per copy, through a gate
  ([gates.md](../gates.md)).
- **Invites as spreading.** Each new member brings space and a new failure
  domain; an invite that both people accept can establish the mutual
  arrangement in one step.

## 5. Design principles this suggests for Casper

1. **Trust replaces metering.** Among friends, skip ledgers and proofs;
   spend that effort on uptime, bandwidth and repair.
2. **Mutual by default, never metered.** The standard arrangement is
   symmetric in spirit ("we keep each other's"), with no balance kept.
   Asymmetry is allowed and unremarkable.
3. **Show giving, never debt.** Visible: what someone holds for others and
   thanks received. Invisible: who is "behind".
4. **Give to the group, place by the system.** Offerings go to a group
   pool; rendezvous-style placement chooses holders; pairwise pins remain
   possible.
5. **State the goal, not the holders.** People set "N copies, on
   different networks"; Casper keeps it true and counts distinct holders.
6. **Every copy has a named keeper.** No orphaned data (the IPFS lesson):
   each copy is a relation tuple a person agreed to, and revocable.
7. **Small contributions count.** Holding a recovery share, being an
   always-on catcher, or inviting someone are all ways to give, so people
   with no spare disk still belong.
8. **One graph, many resources.** Keep "member of", "offers X to", "holds
   for" as general tuples so the next resource reuses them.

## 6. Name ideas for the mutual arrangement

Plain:
- **Pact**: short, two-sided, a promise; "Sam and I have a pact."
- **Circle**: a small group of people who look after each other; scales
  from two to many.
- **Keepers**: names the role; "my keepers" and "I keep for Sam" both read
  well.
- **Swap**: honest about mutuality, but sounds transactional.
- **Safekeeping**: what it is, warmly; long for a button.

Ghost-themed:
- **Haunt**: the place your friendly ghosts live ("join my haunt");
  playful, fits Casper.
- **Kindred spirits**: the friendly twin of "ghost"; describes the
  relationship, not the mechanics.
- **Spirit pact**: Pact with a wink; a bit solemn for everyday use.
- **Boo buddies**: very playful; might read as childish.
- **Poltergeist pals**: memorable, too long.
- **Fellow ghosts**: gentle, slightly melancholy.

A front-runner pair to test: **Pact** (plain, the arrangement) inside a
**Haunt** (playful, the group).
